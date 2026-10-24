"""팀 프로젝트 팀 편성 앱 (Streamlit + Supabase + OpenRouter)"""
import os
import re
import threading
import time
import uuid
from contextlib import suppress
from zoneinfo import ZoneInfo

import bcrypt
import psycopg2
import requests
import streamlit as st
from dotenv import load_dotenv
from psycopg2 import errors as pgerr
from psycopg2 import pool
from psycopg2.extras import RealDictCursor, execute_values

from rules import (apply_block, build_prompt, diagnose, parse_proposal, parse_roster,
                   solve, validate)

load_dotenv()
st.set_page_config(page_title="팀 편성", page_icon="👥", layout="wide")

AI_TIMEOUT = 120  # 모델 하나당 전체 대기 시간(초)
DEFAULT_MODELS = [  # 1차, 2차, 3차. 본인이 고른 :free 모델로 바꾸거나 AI_MODEL_1~3 비밀 값으로 지정
    "nvidia/nemotron-3-super-120b-a12b:free",
    "openai/gpt-oss-120b:free",
    "z-ai/glm-4.5-air:free",
]


# ------------------------------------------------------------------ 비밀 값, DB
def secret(name):
    v = os.getenv(name)
    if v:
        return v
    with suppress(Exception):
        return st.secrets.get(name)
    return None


@st.cache_resource
def get_pool():
    """DB 연결은 한 번만 만들고(풀) 계속 재사용함."""
    return pool.ThreadedConnectionPool(1, 6, secret("DATABASE_URL"), connect_timeout=10)


def tx(fn, lock=False):
    """트랜잭션 하나로 fn(cur)을 실행. lock=True면 쓰기 작업을 한 줄로 세움(advisory lock)."""
    p = get_pool()
    for attempt in range(2):
        conn = p.getconn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                if lock:
                    cur.execute("select pg_advisory_xact_lock(7001)")
                out = fn(cur)
            conn.commit()
            p.putconn(conn)
            return out
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            p.putconn(conn, close=True)  # 끊어진 연결은 버리고 한 번 더 시도
            if attempt:
                raise
        except Exception:
            conn.rollback()
            p.putconn(conn)
            raise


@st.cache_resource(show_spinner="DB 준비 중...")
def init_db():
    schema = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql"), encoding="utf-8").read()
    pw = secret("ADMIN_PASSWORD")
    if not pw:
        raise RuntimeError("ADMIN_PASSWORD 없음")

    def work(cur):
        cur.execute(schema)
        cur.execute("select id, password_hash from tf_students where student_no = 'admin'")
        r = cur.fetchone()
        if r is None:
            cur.execute("insert into tf_students(student_no, name, password_hash, is_admin) values ('admin','관리자',%s,true)",
                        (hash_pw(pw),))
        elif not bcrypt.checkpw(pw.encode()[:72], r["password_hash"].encode()):
            cur.execute("update tf_students set password_hash = %s where id = %s", (hash_pw(pw), r["id"]))

    tx(work, lock=True)
    return True


def hash_pw(pw):
    return bcrypt.hashpw(pw.encode()[:72], bcrypt.gensalt(10)).decode()


# ------------------------------------------------------------------ 상태 읽기
def load_state(cur):
    cur.execute("select id, student_no, name, main_role as main from tf_students where not is_admin order by student_no")
    studs = {r["id"]: {**r, "subs": []} for r in cur.fetchall()}
    cur.execute("select student_id, role from tf_student_sub_roles order by role")
    for r in cur.fetchall():
        if r["student_id"] in studs:
            studs[r["student_id"]]["subs"].append(r["role"])
    cur.execute("select id, name from tf_teams order by id")
    teams = [{"id": r["id"], "name": r["name"], "no": int(re.sub(r"\D", "", r["name"]) or 0),
              "members": [], "slots": []} for r in cur.fetchall()]
    by_id = {t["id"]: t for t in teams}
    team_of = {}
    cur.execute("select team_id, slot_no, student_id from tf_members order by team_id, slot_no")
    for r in cur.fetchall():
        by_id[r["team_id"]]["members"].append(r["student_id"])
        by_id[r["team_id"]]["slots"].append(r["slot_no"])
        team_of[r["student_id"]] = r["team_id"]
    cur.execute("select id, team_id, student_id, status, reason, created_at from tf_applications order by id")
    apps = [dict(r) for r in cur.fetchall()]
    votes = {}
    cur.execute("select application_id, voter_id, vote from tf_votes")
    for r in cur.fetchall():
        votes.setdefault(r["application_id"], {})[r["voter_id"]] = r["vote"]
    cur.execute("select value from tf_settings where key = 'closed'")
    r = cur.fetchone()
    return {"studs": studs, "teams": teams, "team_of": team_of, "apps": apps, "votes": votes,
            "closed": bool(r and r["value"] == "1")}


def get_user(cur, uid):
    cur.execute("select id, student_no, name, is_admin from tf_students where id = %s", (uid,))
    return cur.fetchone()


def make_snap(s):
    sd = lambda x: {"id": x["id"], "student_no": x["student_no"], "name": x["name"], "main": x["main"], "subs": x["subs"]}
    return {"teams": {t["name"]: [sd(s["studs"][i]) for i in t["members"]] for t in s["teams"]},
            "unassigned": [sd(x) for i, x in s["studs"].items() if i not in s["team_of"]]}


def add_member(cur, team, sid):
    slot = sorted(set(range(1, 5)) - set(team["slots"]))[0]
    cur.execute("insert into tf_members(team_id, slot_no, student_id) values (%s,%s,%s)", (team["id"], slot, sid))
    team["slots"].append(slot)
    team["members"].append(sid)


# ------------------------------------------------------------------ 동작 (모두 (결과 첫 단어, 안내) 반환)
def do_apply(cur, uid, team_id):
    s = load_state(cur)
    if s["closed"]:
        return "막힘", "모집이 마감됨"
    if uid in s["team_of"]:
        return "막힘", "이미 팀이 있음"
    if any(a["student_id"] == uid and a["status"] == "대기" for a in s["apps"]):
        return "막힘", "결과를 기다리는 지원이 있음"
    team = next((t for t in s["teams"] if t["id"] == team_id), None)
    me = s["studs"].get(uid)
    if team is None or me is None:
        return "막힘", "없는 팀이거나 명단에 없는 학생임"
    members = [s["studs"][i] for i in team["members"]]
    why = apply_block(members, me)
    if why:
        return "막힘", why
    status = "승인" if not members else "대기"
    cur.execute("insert into tf_applications(team_id, student_id, status, reason) values (%s,%s,%s,%s)",
                (team_id, uid, status, "빈 팀에 지원" if status == "승인" else None))
    if status == "승인":
        add_member(cur, team, uid)
        return "승인", f"{team['name']}의 첫 팀원이 됨(투표 없이 승인)"
    return "대기", f"{team['name']} 팀원의 투표를 기다림"


def do_vote(cur, voter, app_id, vote):
    s = load_state(cur)
    app = next((a for a in s["apps"] if a["id"] == app_id), None)
    if app is None:
        return "막힘", "없는 지원임"
    if app["status"] != "대기":
        return app["status"], "이미 결과가 나온 지원임"
    team = next(t for t in s["teams"] if t["id"] == app["team_id"])
    if s["closed"] or voter not in team["members"]:
        return "막힘", "이 지원에 투표할 수 없음"
    if voter in s["votes"].get(app_id, {}):
        return "막힘", "이미 투표함(바꿀 수 없음)"
    cur.execute("insert into tf_votes(application_id, voter_id, vote) values (%s,%s,%s)", (app_id, voter, vote))
    if vote == "거부":
        cur.execute("update tf_applications set status='거절', reason='팀원이 거부함' where id=%s", (app_id,))
        return "거절", "팀원이 거부해 바로 거절됨"
    voted = {**s["votes"].get(app_id, {}), voter: vote}
    if not all(voted.get(m) == "찬성" for m in team["members"]):
        return "대기", "아직 투표하지 않은 팀원이 있음"
    # 모든 팀원이 찬성 -> 승인 직전에 규칙을 다시 검사 (처리 순서 = 투표가 끝난 순서)
    applicant = s["studs"][app["student_id"]]
    why = "이미 다른 팀에 속함" if applicant["id"] in s["team_of"] else apply_block([s["studs"][i] for i in team["members"]], applicant)
    if why:
        cur.execute("update tf_applications set status='거절', reason=%s where id=%s", (f"승인 직전 검사: {why}", app_id))
        return "거절", f"승인 직전 검사에서 규칙을 어김({why})"
    add_member(cur, team, applicant["id"])
    cur.execute("update tf_applications set status='승인', reason='모든 팀원이 찬성' where id=%s", (app_id,))
    return "승인", f"모든 팀원이 찬성해 {team['name']}의 팀원이 됨"


def do_close(cur):
    s = load_state(cur)
    if s["closed"]:
        return "막힘", "이미 마감됨"
    cur.execute("insert into tf_settings(key, value) values ('closed','1') on conflict (key) do update set value = excluded.value")
    cur.execute("update tf_applications set status='거절', reason='모집 마감' where status='대기'")
    cur.execute("delete from tf_members where team_id in (select team_id from tf_members group by team_id having count(*) = 1)")
    return "승인", "모집을 마감함"


def do_start(cur, rows, hashes, n_teams):
    for t in ("tf_votes", "tf_applications", "tf_members", "tf_teams", "tf_settings"):
        cur.execute(f"delete from {t}")
    cur.execute("delete from tf_students where not is_admin")
    data = [(r["student_no"], r["name"], r["main"], h, False) for r, h in zip(rows, hashes)]
    ids = execute_values(cur, "insert into tf_students(student_no, name, main_role, password_hash, is_admin) values %s returning id, student_no",
                         data, fetch=True)
    idmap = {r["student_no"]: r["id"] for r in ids}
    subs = [(idmap[r["student_no"]], x) for r in rows for x in r["subs"]]
    if subs:
        execute_values(cur, "insert into tf_student_sub_roles(student_id, role) values %s", subs)
    execute_values(cur, "insert into tf_teams(name) values %s", [(f"팀{i}",) for i in range(1, n_teams + 1)])
    cur.execute("insert into tf_settings(key, value) values ('closed','0')")
    return "승인", f"학생 {len(rows)}명과 빈 팀 {n_teams}개로 처음 상태를 만듦"


def do_confirm(cur, prop):
    s = load_state(cur)
    if not s["closed"]:
        return "막힘", "모집을 먼저 마감해야 함"
    snap = make_snap(s)
    err = validate(snap, prop)
    if err:
        return "막힘", f"배정안이 규칙을 어김: {err}"
    ids = {x["student_no"]: x["id"] for x in snap["unassigned"]}
    for t in s["teams"]:
        for no in prop.get(t["name"], []):
            add_member(cur, t, ids[no])
    return "승인", "배정안을 확정함"


# ------------------------------------------------------------------ 콜백
def set_result(res):
    st.session_state["result"] = f"{res[0]}: {res[1]}"


def run_action(fn):
    try:
        return tx(fn, lock=True)
    except pgerr.UniqueViolation:
        return "막힘", "다른 학생이 먼저 그 자리를 차지함"
    except Exception as e:  # 비밀 값이 새지 않도록 오류 종류만 보여 줌
        return "막힘", f"처리 중 오류({type(e).__name__})"


def admin_only(fn):
    def wrapper(*a):
        me = tx(lambda cur: get_user(cur, st.session_state.get("uid")))
        if not me or not me["is_admin"]:
            return set_result(("막힘", "관리자만 쓸 수 있음"))
        return fn(*a)
    return wrapper


def act_apply(uid, team_id):
    set_result(run_action(lambda cur: do_apply(cur, uid, team_id)))


def act_vote(uid, app_id, vote):
    set_result(run_action(lambda cur: do_vote(cur, uid, app_id, vote)))


@admin_only
def act_close():
    set_result(run_action(do_close))


@admin_only
def act_start():
    f = st.session_state.get("admin_roster_file")
    if f is None:
        return set_result(("막힘", "명단 CSV 파일을 먼저 올려 주세요"))
    rows, err = parse_roster(f.getvalue())
    if err:
        return set_result(("막힘", err))
    n = int(st.session_state.get("admin_team_count") or 3)
    hashes = [hash_pw("1234") for _ in rows]
    res = run_action(lambda cur: do_start(cur, rows, hashes, n))
    if res[0] == "승인":
        reset_job()
    set_result(res)


@admin_only
def act_confirm():
    J = job()
    if J["state"] != "done" or J["proposal"] is None:
        return set_result(("막힘", "확정할 배정안이 없음"))
    res = run_action(lambda cur: do_confirm(cur, J["proposal"]))
    if res[0] == "승인":
        J["state"] = "confirmed"
    set_result(res)


def logout():
    st.session_state.pop("uid", None)
    st.session_state.pop("result", None)


# ------------------------------------------------------------------ AI 배정 (백그라운드 스레드)
@st.cache_resource
def job():
    return {"id": "", "state": "idle", "stage": 0, "model": "", "started": 0.0,
            "log": [], "proposal": None, "source": "", "message": ""}


def reset_job():
    job().update(id=uuid.uuid4().hex, state="idle", stage=0, log=[], proposal=None, source="", message="")


def log_ai(model, outcome, detail):
    with suppress(Exception):
        tx(lambda cur: cur.execute("insert into tf_ai_logs(model, outcome, detail) values (%s,%s,%s)", (model, outcome, detail)))


def ask(model, prompt, key):
    """모델 하나를 부름. 응답이 시작된 뒤 천천히 오는 경우까지 포함해 전체 120초가 넘으면 끊음."""
    box = {}

    def work():
        try:
            r = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": model, "messages": [{"role": "user", "content": prompt}],
                      "max_tokens": 4000, "temperature": 0, "reasoning": {"effort": "low"}},
                timeout=(10, AI_TIMEOUT + 5))
            if r.status_code != 200:
                box["err"] = f"HTTP {r.status_code}"
                return
            j = r.json()
            if "choices" not in j:
                box["err"] = "API 오류 응답"
                return
            box["text"] = ((j["choices"][0].get("message") or {}).get("content") or "").strip()
        except Exception as e:
            box["err"] = type(e).__name__

    th = threading.Thread(target=work, daemon=True)
    th.start()
    th.join(AI_TIMEOUT)
    if th.is_alive():
        return None, f"{AI_TIMEOUT}초 안에 응답하지 않음"
    if "err" in box:
        return None, box["err"]
    return (box["text"], None) if box.get("text") else (None, "빈 응답")


def run_job(J, jid, snap, sol, key, models):
    try:
        prompt = build_prompt(snap)
        for stage, model in enumerate(models, 1):
            if J["id"] != jid:
                return
            J.update(stage=stage, model=model, started=time.time())
            text, err = ask(model, prompt, key)
            prop = None
            if err is None:
                prop = parse_proposal(text)
                if prop is None:
                    err = "JSON 형식 오류"
                elif (bad := validate(snap, prop)):
                    err = f"배정안 검사 실패({bad})"
            if err is None:
                log_ai(model, "성공", "")
                if J["id"] == jid:
                    J.update(state="done", proposal=prop, source=f"AI {stage}차 모델({model})")
                return
            log_ai(model, "실패", err)
            nxt = "다음 모델로 넘어감" if stage < len(models) else "앱이 직접 배정함"
            J["log"].append(f"{stage}차 모델({model}) 실패: {err} → {nxt}")
    except Exception as e:
        J["log"].append(f"AI 배정 중 오류({type(e).__name__}) → 앱이 직접 배정함")
    if J["id"] == jid:
        J.update(state="done", proposal=sol, source="앱 자체 배정(완전 탐색)")


@admin_only
def act_assign():
    J = job()
    if J["state"] == "running":
        return set_result(("막힘", "이미 AI 배정이 진행 중임"))
    state = tx(load_state)
    if not state["closed"]:
        return set_result(("막힘", "모집을 먼저 마감해야 함"))
    snap = make_snap(state)
    sol = solve(snap)
    jid = uuid.uuid4().hex
    if sol is None:  # 규칙을 지키는 배정이 없으면 AI를 부르지 않음
        J.update(id=jid, state="infeasible", message=diagnose(snap), proposal=None, log=[])
        return set_result(("막힘", "규칙을 지키는 배정이 없음"))
    if not st.session_state.get("admin_use_ai", True):  # AI를 부르지 않고 앱이 바로 배정
        J.update(id=jid, state="done", proposal=sol, source="앱 자체 배정(AI를 부르지 않음)", log=[])
        return set_result(("승인", "AI를 부르지 않고 앱이 배정함"))
    models = [secret(f"AI_MODEL_{i + 1}") or m for i, m in enumerate(DEFAULT_MODELS)]
    J.update(id=jid, state="running", stage=1, model=models[0], started=time.time(), log=[], proposal=None, message="")
    threading.Thread(target=run_job, args=(J, jid, snap, sol, secret("OPENROUTER_API_KEY"), models), daemon=True).start()
    set_result(("승인", "AI 배정을 시작함"))


# ------------------------------------------------------------------ 화면
KST = ZoneInfo("Asia/Seoul")  # 배포 서버는 UTC이므로 한국 시각으로 바꿔 보여 줌


def fmt(s):
    return f"{s['name']} · 주 역할 {s['main']} · 보조 역할 {', '.join(s['subs']) or '없음'}"


def kst(ts):
    return ts.astimezone(KST).strftime("%m-%d %H:%M:%S") if ts else ""


def show_result():
    """단추를 누른 결과. 첫 단어(승인/대기/거절/막힘)에 따라 색을 달리함."""
    msg = st.session_state.get("result", "")
    with st.container(key="result"):
        if not msg:
            st.caption("단추를 누르면 결과가 여기에 나옴")
        else:
            {"승인": st.success, "대기": st.info, "거절": st.error, "막힘": st.warning}.get(msg.split(":", 1)[0], st.write)(msg)


def render_teams(s, me):
    my_team = s["team_of"].get(me["id"])
    my_pending = any(a["student_id"] == me["id"] and a["status"] == "대기" for a in s["apps"])
    tname = {t["id"]: t["name"] for t in s["teams"]}
    blocked = None
    if not me["is_admin"]:
        if my_team is not None:
            blocked = f"이미 {tname[my_team]}에 속해 있습니다."
        elif my_pending:
            blocked = "결과를 기다리는 지원이 있습니다."
        elif s["closed"]:
            blocked = "모집이 마감되었습니다."
        if blocked:
            st.caption(f"지원 단추를 쓸 수 없음: {blocked}")
    cols = st.columns(max(1, min(len(s["teams"]), 4)))
    for i, t in enumerate(s["teams"]):
        with cols[i % len(cols)]:
            with st.container(key=f"team_{t['no']}", border=True):
                mine = (not me["is_admin"]) and my_team == t["id"]
                st.markdown(f"**{t['name']}**" + (" :blue-background[내 팀]" if mine else "") + f" ({len(t['members'])}/4명)")
                for sid in t["members"]:
                    st.markdown("- " + fmt(s["studs"][sid]))
                if not t["members"]:
                    st.write("빈 팀")
                if not me["is_admin"]:
                    st.button(f"{t['name']}에 지원", key=f"apply_{t['no']}", disabled=blocked is not None,
                              on_click=act_apply, args=(me["id"], t["id"]))
    if my_team is not None and not me["is_admin"]:  # 내 팀 강조
        no = next(t["no"] for t in s["teams"] if t["id"] == my_team)
        st.markdown(f"<style>.st-key-team_{no}{{border:2px solid #1c83e1 !important;background:#eaf2fd;}}</style>",
                    unsafe_allow_html=True)


def render_student(s, me):
    my_team = s["team_of"].get(me["id"])
    tname = {t["id"]: t["name"] for t in s["teams"]}
    st.write(f"모집 상태: **{'마감' if s['closed'] else '모집 중'}** · 내 팀: **{tname.get(my_team, '없음')}** · 화면은 4초마다 저절로 바뀜")
    if my_team is None:
        st.info("아직 팀이 없음")
    else:
        st.subheader("내 팀에 들어온 지원")
        pend = [a for a in s["apps"] if a["team_id"] == my_team and a["status"] == "대기"]
        if not pend:
            st.caption("대기 중인 지원 없음")
        for a in pend:
            ap = s["studs"][a["student_id"]]
            c0, c1, c2 = st.columns([8, 1, 1])
            c0.write(f"{ap['name']} · 학번 {ap['student_no']} · 주 역할 {ap['main']} · 보조 역할 "
                     f"{', '.join(ap['subs']) or '없음'} · {kst(a['created_at'])}")
            mv = s["votes"].get(a["id"], {}).get(me["id"])
            if mv:
                c1.write(f"내 투표: {mv}")
            elif not s["closed"]:
                c1.button("찬성", key=f"vote_yes_{ap['student_no']}", on_click=act_vote, args=(me["id"], a["id"], "찬성"))
                c2.button("거부", key=f"vote_no_{ap['student_no']}", on_click=act_vote, args=(me["id"], a["id"], "거부"))
    st.subheader("팀 목록")
    render_teams(s, me)
    st.subheader("내 지원 기록")
    mine = [a for a in s["apps"] if a["student_id"] == me["id"]]
    if not mine:
        st.caption("지원 기록 없음")
    else:
        st.table([{"팀": tname[a["team_id"]], "상태": a["status"], "사유": a["reason"] or "팀원 투표 대기",
                   "시각": kst(a["created_at"])} for a in reversed(mine)])


def render_admin(s, me):
    J = job()
    st.subheader("명단 올리기")
    st.caption("명단 CSV(UTF-8, 머리글 `student_no,name,main_role,sub_roles`)와 팀 수를 넣고 단추를 누르면 "
               "지원, 투표, 팀원, 마감 표시를 모두 지우고 처음 상태를 만듭니다.")
    st.file_uploader("명단 CSV", type=["csv"], key="admin_roster_file")
    st.number_input("팀 수", min_value=1, max_value=10, value=3, step=1, key="admin_team_count")
    st.button("명단으로 처음 상태 만들기", key="admin_start", on_click=act_start)
    st.divider()
    st.subheader("마감과 배정")
    st.write(f"모집 상태: **{'마감' if s['closed'] else '모집 중'}**")
    st.button("모집 마감", key="admin_close", type="primary", on_click=act_close)
    with st.container(key="unassigned"):
        un = [x["name"] for i, x in s["studs"].items() if i not in s["team_of"]]
        st.write(f"팀이 없는 학생 {len(un)}명: " + (", ".join(un) or "없음"))
    st.checkbox("AI를 부름(끄면 앱이 바로 배정함)", value=True, key="admin_use_ai")
    st.button("AI 배정", key="admin_assign", type="primary", on_click=act_assign)
    with st.container(key="assign_status"):
        stt = J["state"]
        if stt == "idle":
            st.write("AI 배정 전")
        elif stt == "running":
            st.write(f"AI가 생각하는 중: {J['stage']}차 모델({J['model']})의 응답을 기다리는 중 "
                     f"({int(time.time() - J['started'])}초 / {AI_TIMEOUT}초)")
        elif stt == "infeasible":
            st.write(f"규칙을 지키는 배정이 없음: {J['message']}")
        elif stt == "done":
            st.write(f"배정안이 준비됨 (출처: {J['source']})")
        elif stt == "confirmed":
            st.write("배정안을 확정함")
        for line in J["log"]:
            st.caption(line)
    if stt == "done" and J["proposal"] is not None:
        snap, prop = make_snap(s), J["proposal"]
        by_no = {x["student_no"]: x for x in snap["unassigned"]}
        for name, ms in snap["teams"].items():
            old = [f"{m['name']}(기존)" for m in ms]
            new = [f"{by_no[n]['name']} [{by_no[n]['main']}]" for n in prop.get(name, [])]
            st.write(f"**{name}**: " + ", ".join(old + new))
        st.button("배정안 확정", key="admin_confirm", type="primary", on_click=act_confirm)
    with st.expander("AI 기록(최근 20건)"):
        logs = tx(lambda cur: (cur.execute("select created_at, model, outcome, detail from tf_ai_logs order by id desc limit 20"),
                               cur.fetchall())[1])
        if not logs:
            st.caption("기록 없음")
        else:
            st.table([{"시각": kst(r["created_at"]), "모델": r["model"], "결과": r["outcome"], "내용": r["detail"] or ""} for r in logs])
    st.subheader("팀 목록")
    render_teams(s, me)


@st.fragment(run_every=4)  # 4초마다 이 부분만 다시 그려 다른 사람의 변경을 보여 줌
def live_view(uid):
    s, me = tx(lambda cur: (load_state(cur), get_user(cur, uid)))
    if me is None:  # 명단을 다시 올려 계정이 사라진 경우
        logout()
        st.rerun(scope="app")
    J = job()
    if me["is_admin"] and J["state"] == "done" and st.session_state.get("assign_seen") != J["id"]:
        st.session_state["assign_seen"] = J["id"]
        st.session_state["result"] = f"승인: 배정안을 받음(출처: {J['source']})"
    show_result()
    (render_admin if me["is_admin"] else render_student)(s, me)


def login_view():
    st.title("팀 편성")
    with st.form("login_form"):
        no = st.text_input("학번", key="login_student_no")
        pw = st.text_input("비밀번호", type="password", key="login_password")
        go = st.form_submit_button("로그인", key="login_submit")
    if go:
        r = tx(lambda cur: (cur.execute("select id, password_hash from tf_students where student_no = %s", (no.strip(),)), cur.fetchone())[1])
        if r and bcrypt.checkpw(pw.encode()[:72], r["password_hash"].encode()):
            st.session_state["uid"] = r["id"]
            st.session_state["result"] = "승인: 로그인함"
            st.rerun()
        st.session_state["result"] = "막힘: 학번 또는 비밀번호가 올바르지 않음"
    show_result()


def main():
    try:
        init_db()
    except Exception as e:
        st.error(f"DB를 준비하지 못함({type(e).__name__}). DATABASE_URL, ADMIN_PASSWORD 설정을 확인해 주세요.")
        st.stop()
    uid = st.session_state.get("uid")
    if uid is None:
        return login_view()
    me = tx(lambda cur: get_user(cur, uid))
    if me is None:
        logout()
        st.rerun()
    with st.sidebar:
        st.button("로그아웃", key="logout", on_click=logout)
    st.title("팀 편성 · 관리자" if me["is_admin"] else "팀 편성")
    with st.container(key="whoami"):
        st.write(f"로그인: **{me['name']}** ({me['student_no']})")
    live_view(uid)


main()
