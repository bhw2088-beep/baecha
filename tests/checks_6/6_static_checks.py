"""
6번 정적 분석 · pytest — tests/checks_6/run_all.py 가 도커 이미지 안에서 실행한다 (검사 도구: requirements-dev.txt)
    단독 실행: 수정본 폴더에서  docker run --rm -v "${PWD}:/src" -w /src baecha-app:dev sh -c
              "pip install -q -r requirements-dev.txt && python tests/checks_6/6_static_checks.py"

CI(.github/workflows/ci.yml lint-test)와 같은 검사를 같은 순서로 돌린다.
  pyflakes(문법 · 미사용 import) → Ruff(버그 · 보안 규칙, ruff.toml) → Bandit(보안, 중간 이상) →
  pip-audit(라이브러리 알려진 취약점) → pytest(서버 없이 도는 점검 전체 · tests/test_suites.py)
"""

import subprocess
import sys

from _common import ROOT, check, finish, section

PY = sys.executable
TARGETS = ["backend", "frontend", "simulator", "tests", "tools"]
STEPS = [
    ("pyflakes — 문법 오류 · 미사용 import", [PY, "-m", "pyflakes", *TARGETS]),
    ("Ruff — 버그 · 보안 규칙 (설정 ruff.toml)", [PY, "-m", "ruff", "check", "--no-cache", *TARGETS]),
    ("Bandit — 보안 정적 분석 (중간 이상 심각도)", [PY, "-m", "bandit", "-q", "-r", "backend", "frontend", "simulator", "tools", "-ll"]),
    ("pip-audit — 라이브러리 알려진 취약점 (requirements.txt)",
     [PY, "-m", "pip_audit", "-r", "requirements.txt", "--progress-spinner", "off"]),
    ("pytest — 서버 없이 도는 점검 전체 (tests/test_suites.py)", [PY, "-m", "pytest", "-q", "-p", "no:cacheprovider"]),
]

section("6. 정적 분석 · pytest (CI lint-test 와 같은 검사)")
for name, cmd in STEPS:
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (r.stdout + r.stderr).strip().splitlines()
    last = next((x.strip() for x in reversed(out) if x.strip() and not x.startswith("=")), "")
    if name.startswith("pytest"):
        last = next((x.strip("= ") for x in reversed(out) if " passed" in x or " failed" in x), last)
    detail = last or "문제 0건"
    check(name, r.returncode == 0, detail[:120])
    if r.returncode != 0:
        for line in out[-15:]:
            print(f"      {line}")

finish()
