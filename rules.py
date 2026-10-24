"""팀 규칙, 배정안 검사, 자체 배정(완전 탐색), 명단 CSV 읽기. streamlit에 의존하지 않음."""
import csv
import io
import json
import re

ROLES = ["기획자", "웹 프로그래머", "프론트엔드 디자이너", "테스터", "보고서 작성자", "발표자"]
PL, WP = ROLES[0], ROLES[1]

# 학생 = {"id", "student_no", "name", "main", "subs": [..]}


def roles(s):
    return {s["main"], *s["subs"]}


def _capable(members, role):
    return [i for i, m in enumerate(members) if role in roles(m)]


def cover(members):
    """기획자/웹 프로그래머를 서로 다른 학생이 맡을 수 있는 정도(0~2)."""
    pl, wp = _capable(members, PL), _capable(members, WP)
    if any(i != j for i in pl for j in wp):
        return 2
    return 1 if (pl or wp) else 0


def missing(members):
    out = []
    if not _capable(members, PL):
        out.append(PL)
    if not _capable(members, WP):
        out.append(WP)
    if not out and cover(members) < 2:
        out.append("서로 다른 학생이 맡는 기획자와 웹 프로그래머")
    return out


def seat_short(members):
    """members(지원자 포함) 기준으로 남는 자리가 채워야 할 필수 역할 수보다 적은가."""
    return 4 - len(members) < 2 - cover(members)


def apply_block(members, me):
    """me가 members 팀에 들어갈 수 없으면 이유, 가능하면 None (지원 접수와 승인 직전 검사에 함께 씀)."""
    if len(members) >= 4:
        return "팀이 꽉 참"
    if me["main"] in [m["main"] for m in members]:
        return "같은 주 역할의 팀원이 있음"
    if seat_short(members + [me]):
        return "필수 포함 역할을 맡을 학생의 자리가 모자람"
    return None


def team_error(members):
    """확정 팀 구성 검사 (2~4명, 주 역할 중복 없음, 필수 포함 역할)."""
    if not 2 <= len(members) <= 4:
        return f"인원이 {len(members)}명임(2~4명이어야 함)"
    mains = [m["main"] for m in members]
    if len(set(mains)) != len(mains):
        return "주 역할이 중복됨"
    if cover(members) < 2:
        return "필수 포함 역할(기획자, 웹 프로그래머)을 채우지 못함"
    return None


def validate(snap, prop):
    """배정안 검사 5가지. snap={'teams':{이름:[학생]}, 'unassigned':[학생]}, prop={팀이름:[학번]}."""
    if not isinstance(prop, dict) or set(prop) - set(snap["teams"]):
        return "모르는 팀 이름이 있음"
    un = {s["student_no"]: s for s in snap["unassigned"]}
    flat = [x for v in prop.values() for x in v]
    if len(flat) != len(set(flat)):
        return "한 학생이 여러 팀에 배정됨"
    if set(flat) != set(un):
        return "빠진 학생이 있거나 배정 대상이 아닌 학생이 있음"
    for name, members in snap["teams"].items():
        e = team_error(members + [un[x] for x in prop.get(name, [])])
        if e:
            return f"{name}: {e}"
    return None


def solve(snap, budget=400000):
    """검사 5가지를 모두 통과하는 배정을 완전 탐색(가지치기)으로 찾음. 없으면 None."""
    names, un = list(snap["teams"]), snap["unassigned"]
    cur = {n: list(m) for n, m in snap["teams"].items()}
    out = {n: [] for n in names}
    left = [budget]

    def rec(i):
        left[0] -= 1
        if left[0] < 0:
            return False
        if sum(max(0, 2 - len(m)) for m in cur.values()) > len(un) - i:
            return False
        if any(seat_short(m) for m in cur.values()):
            return False
        if i == len(un):
            return all(team_error(m) is None for m in cur.values())
        s = un[i]
        for n in sorted(names, key=lambda n: len(cur[n])):
            m = cur[n]
            if len(m) >= 4 or s["main"] in [x["main"] for x in m]:
                continue
            m.append(s)
            out[n].append(s["student_no"])
            if rec(i + 1):
                return True
            m.pop()
            out[n].pop()
        return False

    return {n: list(v) for n, v in out.items()} if rec(0) else None


def diagnose(snap):
    """규칙을 지키는 배정이 없을 때 보여 줄 이유."""
    un, out = snap["unassigned"], []
    for name, ms in snap["teams"].items():
        if len(ms) >= 4:
            miss = missing(ms)
            if miss:
                out.append(f"{name}은 4명이 다 찼는데 {' · '.join(miss)} 역할을 채울 수 없음")
        else:
            for role in (PL, WP):
                if not _capable(ms + un, role):
                    out.append(f"{name}에 {role}를 맡을 학생이 없음")
    return "; ".join(out) or "인원(2~4명), 주 역할 중복, 필수 포함 역할을 한꺼번에 지키는 배정이 없음"


def parse_roster(raw):
    """명단 CSV(UTF-8, BOM 가능) -> (rows, 오류문구)."""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None, "UTF-8 파일이 아님"
    rd = csv.DictReader(io.StringIO(text))
    if rd.fieldnames:
        rd.fieldnames = [h.strip() for h in rd.fieldnames]
    if not rd.fieldnames or not {"student_no", "name", "main_role", "sub_roles"} <= set(rd.fieldnames):
        return None, "열 이름은 student_no,name,main_role,sub_roles 여야 함"
    rows, seen = [], set()
    for line, r in enumerate(rd, 2):
        no, name = (r.get("student_no") or "").strip(), (r.get("name") or "").strip()
        main = (r.get("main_role") or "").strip()
        subs = [x.strip() for x in (r.get("sub_roles") or "").split(";") if x.strip()]
        if not no or not name or no == "admin" or no in seen:
            return None, f"{line}번째 줄: 학번이 비었거나 중복이거나 admin 임"
        if main not in ROLES or any(x not in ROLES for x in subs):
            return None, f"{line}번째 줄: 모르는 역할이 있음"
        if len(subs) > 2 or len(set(subs)) != len(subs) or main in subs:
            return None, f"{line}번째 줄: 보조 역할은 주 역할과 다른 0~2개여야 함"
        seen.add(no)
        rows.append({"student_no": no, "name": name, "main": main, "subs": subs})
    return (rows, None) if rows else (None, "학생이 한 명도 없음")


def build_prompt(snap):
    d = lambda s: f'{s["student_no"]} {s["name"]} (주 역할: {s["main"]}, 보조 역할: {", ".join(s["subs"]) or "없음"})'
    lines = ["팀 프로젝트 팀 편성입니다. 팀이 없는 학생을 팀에 배정하세요.", "", "[현재 팀 구성]"]
    for n, ms in snap["teams"].items():
        lines.append(f"{n}: " + (" / ".join(d(m) for m in ms) or "(비어 있음)"))
    lines += ["", "[팀이 없는 학생]"] + [d(s) for s in snap["unassigned"]]
    lines += [
        "", "[규칙]",
        "- 팀이 없는 학생만 배정하고, 이미 팀에 있는 학생은 옮기지 않는다.",
        "- 모든 학생은 정확히 한 팀에만 배정한다.",
        "- 배정 뒤 모든 팀은 2~4명이어야 한다(빈 팀 없음).",
        "- 한 팀에 주 역할이 같은 학생이 2명 이상이면 안 된다.",
        "- 모든 팀에는 기획자를 맡을 학생과 웹 프로그래머를 맡을 학생이 서로 다른 사람으로 1명씩 있어야 한다. 주 역할이나 보조 역할로 맡을 수 있다.",
        "", '출력: 설명 없이 JSON 객체 하나만. 키는 팀 이름, 값은 새로 배정할 학번의 배열. 예: {"팀1": ["2026101"], "팀2": []}',
    ]
    return "\n".join(lines)


def parse_proposal(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except ValueError:
        return None
    if isinstance(d, dict) and len(d) == 1 and isinstance(next(iter(d.values())), dict):
        d = next(iter(d.values()))
    if not isinstance(d, dict) or any(not isinstance(v, list) for v in d.values()):
        return None
    return {str(k): [str(x) for x in v] for k, v in d.items()}
