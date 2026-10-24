"""python test_rules.py 로 실행. 연습용 시나리오의 규칙 판정을 DB 없이 확인함."""
from rules import apply_block, diagnose, parse_roster, parse_proposal, solve, validate, team_error

A = {"PL": "기획자", "WP": "웹 프로그래머", "FD": "프론트엔드 디자이너", "TT": "테스터", "RW": "보고서 작성자", "PT": "발표자"}


def S(no, name, main, *subs):
    return {"id": no, "student_no": str(no), "name": name, "main": A[main], "subs": [A[x] for x in subs]}


# --- 지원 단계 규칙 (부록 2 중간, 부록 3 어려움)
s1 = S(1, "학생1", "PL", "PT", "TT")
s2 = S(2, "학생2", "PL", "TT")
assert apply_block([s1, S(5, "학생5", "WP", "TT")], s2) == "같은 주 역할의 팀원이 있음"  # 중간 3
# 어려움 11: 팀3(테스터, 발표자)에 보고서 작성자가 지원 -> 자리 부족
t3 = [S(11, "학생11", "TT", "PT"), S(15, "학생15", "PT", "FD")]
assert apply_block(t3, S(14, "학생14", "RW", "TT")) == "필수 포함 역할을 맡을 학생의 자리가 모자람"
assert apply_block([S(11, "학생11", "TT", "PT")], S(15, "학생15", "PT", "FD")) is None  # 어려움 10
# 공고 예: 기획자(보조 웹)만 있는 팀 = 필수 역할 1개만 채움
solo = [S(1, "a", "PL", "WP")]
assert apply_block(solo, S(2, "b", "TT")) is None and apply_block(solo + [S(3, "c", "FD")], S(4, "d", "RW")) is None
assert apply_block(solo + [S(3, "c", "FD"), S(5, "e", "RW")], S(4, "d", "PT")) is not None  # 4명이 되면 자리 없음
# 어려움 8: 승인 직전 재검사 -> 팀2에 이미 웹 프로그래머(학생5)가 들어와 학생6 거절
t2 = [S(2, "학생2", "PL", "PT"), S(8, "학생8", "FD", "TT"), S(5, "학생5", "WP", "FD")]
assert apply_block(t2, S(6, "학생6", "WP", "RW")) == "같은 주 역할의 팀원이 있음"

# --- 배정 (어려움 14 이후 상태)
snap = {
    "teams": {
        "팀1": [S(1, "학생1", "PL", "RW"), S(4, "학생4", "WP", "PL"), S(7, "학생7", "FD", "PT", "TT")],
        "팀2": t2,
        "팀3": t3,
        "팀4": [],
    },
    "unassigned": [S(3, "학생3", "PL", "TT"), S(6, "학생6", "WP", "RW"), S(9, "학생9", "FD", "WP"),
                   S(10, "학생10", "TT", "RW"), S(12, "학생12", "TT", "PL"), S(13, "학생13", "RW", "PT", "FD"),
                   S(14, "학생14", "RW", "TT"), S(16, "학생16", "PT")],
}
sol = solve(snap)
assert sol is not None and validate(snap, sol) is None, sol
assert validate(snap, {"팀1": ["3", "3"]}) is not None  # 중복 배정 거름
assert validate(snap, {"팀4": ["3"]}) is not None  # 빠진 학생 거름

# --- 규칙을 지키는 배정이 없는 경우: 팀 하나에 웹 프로그래머를 맡을 학생이 없음
bad = {"teams": {"팀1": [S(1, "a", "PL"), S(2, "b", "TT")], "팀2": []},
       "unassigned": [S(3, "c", "PL"), S(4, "d", "TT"), S(5, "e", "RW", "PL")]}
assert solve(bad) is None
assert "웹 프로그래머를 맡을 학생이 없음" in diagnose(bad), diagnose(bad)
# 4명이 다 찬 팀이 웹 프로그래머를 못 채움
full = {"teams": {"팀1": [S(i, f"x{i}", r) for i, r in enumerate(["PL", "TT", "RW", "PT"])]}, "unassigned": []}
assert solve(full) is None and "4명이 다 찼는데" in diagnose(full)

# --- 명단 CSV (BOM 포함) 와 AI 응답 파싱
raw = "\ufeffstudent_no,name,main_role,sub_roles\n2026001,가나다,기획자,보고서 작성자\n2026002,라마바,웹 프로그래머,\n2026003,사아자,테스터,기획자;발표자\n".encode("utf-8")
rows, err = parse_roster(raw)
assert err is None and len(rows) == 3 and rows[2]["subs"] == ["기획자", "발표자"] and rows[1]["subs"] == []
assert parse_roster("student_no,name,main_role,sub_roles\n1,a,없는역할,\n".encode())[1] is not None
assert parse_proposal('설명 {"팀1": ["1"], "팀2": []} 끝') == {"팀1": ["1"], "팀2": []}
assert parse_proposal("json 없음") is None
assert team_error([s1]) is not None
print("모든 테스트 통과")
