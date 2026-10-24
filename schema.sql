-- 팀 편성 앱 테이블 (모두 tf_ 로 시작). 여러 번 실행해도 안전함.

create table if not exists tf_students (
  id serial primary key,
  student_no text not null unique,
  name text not null,
  main_role text check (main_role in ('기획자','웹 프로그래머','프론트엔드 디자이너','테스터','보고서 작성자','발표자')),
  password_hash text not null,            -- bcrypt 해시 (평문 저장 안 함)
  is_admin boolean not null default false
);

create table if not exists tf_student_sub_roles (
  student_id int not null references tf_students(id) on delete cascade,
  role text not null check (role in ('기획자','웹 프로그래머','프론트엔드 디자이너','테스터','보고서 작성자','발표자')),
  primary key (student_id, role)
);

create table if not exists tf_teams (
  id serial primary key,
  name text not null unique
);

-- 유일성 제약: (team_id, slot_no) 기본키 = 같은 자리에 먼저 도착한 학생만 기록,
-- student_id unique = 한 학생은 한 팀에만 소속
create table if not exists tf_members (
  team_id int not null references tf_teams(id) on delete cascade,
  slot_no int not null check (slot_no between 1 and 4),
  student_id int not null unique references tf_students(id) on delete cascade,
  primary key (team_id, slot_no)
);

create table if not exists tf_applications (
  id serial primary key,
  team_id int not null references tf_teams(id) on delete cascade,
  student_id int not null references tf_students(id) on delete cascade,
  status text not null check (status in ('대기','승인','거절')),
  reason text,
  created_at timestamptz not null default now()
);
-- 결과가 나오기 전에는 학생당 대기 지원 1개
create unique index if not exists tf_applications_one_pending
  on tf_applications(student_id) where status = '대기';

create table if not exists tf_votes (
  application_id int not null references tf_applications(id) on delete cascade,
  voter_id int not null references tf_students(id) on delete cascade,
  vote text not null check (vote in ('찬성','거부')),
  primary key (application_id, voter_id)
);

create table if not exists tf_settings (
  key text primary key,
  value text not null
);

-- AI 호출 기록 (README 실패 사례 근거용)
create table if not exists tf_ai_logs (
  id serial primary key,
  model text,
  outcome text,
  detail text,
  created_at timestamptz not null default now()
);
