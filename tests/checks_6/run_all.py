"""
추가 검증 6가지 한 번에 실행 — 결과는 이 파일 맨 아래에 자동 저장                                약 8~12분

VS Code: 이 파일을 열고 오른쪽 위 ▷ (Run Python File) 를 누르면 1번부터 6번까지 차례로 실행된다.
  - Docker Desktop 이 켜져 있어야 함 (도커 이미지 baecha-app:dev · redis:7.4-alpine)
  - 프로젝트 라이브러리가 없는 파이썬(컴퓨터 기본 파이썬)이어도 됨 → 각 테스트가 도커 안에서 대신 실행됨
  - 데모 스택(8000 · 8501) · 실제 DB 데이터는 건드리지 않음 (테스트 전용 스택 · 컨테이너를 띄우고 끝나면 지움)
터미널: 수정본 폴더에서  python tests/checks_6/run_all.py              (전체)
                         python tests/checks_6/run_all.py --only 3,4,5  (골라서 · 쉼표로 번호)
                         python tests/checks_6/run_all.py --check       (실행 준비만 확인 · 테스트는 안 돌림)

이 폴더(tests/checks_6)의 테스트 6가지 — 파일마다 따로 ▷ 로 실행해도 된다
  1_concurrency.py     동시성: 중복 배차 · 중복 결제 · 중복 화물 등록(Idempotency-Key)          약 2~3분
  2_redis_recovery.py  Redis 장애 복구: GPS 스트림 미처리 메시지 복구 · Celery 작업 재실행      약 1~2분
  3_packing_rules.py   적재 검증: 규칙 R0~R10 경계값 · 엔진 · 검증기                           수 초
  4_matching_ai.py     매칭 AI: LightGBM 시드 5개 · 임계값 0.75 · 재현성 · 단조 제약 · 연동      약 10초
  5_ai_control.py      AI 관제: 함수 권한 · 프롬프트 인젝션 · 전화번호 가림 · 장애 대체         수 초
  6_static_checks.py   정적 분석 · pytest: pyflakes · Ruff · Bandit · pip-audit · pytest (CI 와 같음) 약 2~3분
  (도우미: _common.py 경로 · _concurrency_compose.yml 1번 전용 · _celery_probe.py 2번 전용)

3 · 4 · 5번은 pytest 자동 목록(tests/test_suites.py)에도 들어 있다. 이 파일은 실행만 맡는다 (표준 라이브러리만 사용).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]   # 수정본
IMAGE = "baecha-app:dev"
RESULT_MARK = "# ===== 마지막 실행 결과 (▷ 실행이 끝나면 자동으로 바뀜 · " + "문자열이라 코드로 실행되지 않음) ====="

STATIC_CMD = ["docker", "run", "--rm", "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "PYTHONUNBUFFERED=1",
              "-e", "RUFF_CACHE_DIR=/tmp/ruff", "-e", "BAECHA_DB=0", "-v", f"{ROOT}:/src", "-w", "/src", IMAGE,
              "sh", "-c", "pip install -q -r requirements-dev.txt > /tmp/pip.log 2>&1 "
                          "|| { echo '  [FAIL] 검사 도구 설치 (requirements-dev.txt)'; tail -5 /tmp/pip.log; exit 1; }; "
                          "python tests/checks_6/6_static_checks.py"]

STEPS = [
    (1, "동시성 (중복 배차 · 중복 결제 · 중복 등록)", [sys.executable, "tests/checks_6/1_concurrency.py"]),
    (2, "Redis 장애 복구 (GPS 스트림 · Celery)", [sys.executable, "tests/checks_6/2_redis_recovery.py"]),
    (3, "적재 검증 (규칙 R0~R10 경계값)", [sys.executable, "tests/checks_6/3_packing_rules.py"]),
    (4, "매칭 AI (LightGBM 시드 · 임계값 · 재현성)", [sys.executable, "tests/checks_6/4_matching_ai.py"]),
    (5, "AI 관제 (함수 권한 · 인젝션 · 개인정보)", [sys.executable, "tests/checks_6/5_ai_control.py"]),
    (6, "정적 분석 · pytest (CI 와 같은 검사)", STATIC_CMD),
]
RESULT_RE = re.compile(r"결과:\s*(\d+)개 통과\s*·\s*(\d+)개 실패")
KEEP_RE = re.compile(r"^(■ |\s*\[(PASS|FAIL)\]|결과:|\s{6}\S)")   # 테스트 형식 줄만 결과 블록에 남김


def _dur(sec: float) -> str:
    return f"{int(sec // 60)}분 {int(sec % 60)}초" if sec >= 60 else f"{sec:.0f}초"


def _env() -> dict:
    # 하위 파이썬 출력을 UTF-8 로 (한글) · 깃 배시에서 도커 경로가 바뀌지 않게
    return {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1", "MSYS_NO_PATHCONV": "1"}


def run_step(num: int, title: str, cmd: list[str]) -> dict:
    print(f"\n{'=' * 78}\n▶ {num}. {title}\n{'=' * 78}", flush=True)
    t0 = time.time()
    lines: list[str] = []
    try:
        proc = subprocess.Popen(cmd, cwd=ROOT, env=_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            print(line, flush=True)
            lines.append(line)
        code = proc.wait()
    except FileNotFoundError as e:
        lines.append(f"  [FAIL] 실행할 수 없음: {e}")
        print(lines[-1])
        code = 1
    m = next((RESULT_RE.search(x) for x in reversed(lines) if RESULT_RE.search(x)), None)
    passed, failed = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    ok = code == 0 and m is not None and failed == 0
    return {"num": num, "title": title, "ok": ok, "passed": passed, "failed": failed, "code": code,
            "sec": time.time() - t0, "lines": [x for x in lines if KEEP_RE.match(x)]}


def summary(results: list[dict], total_sec: float) -> str:
    ok = sum(r["ok"] for r in results)
    out = [f"## 추가 검증 테스트 {len(results)}가지 — {ok}/{len(results)} 통과 · 걸린 시간 {_dur(total_sec)}", "",
           "| # | 테스트 | 결과 | 항목 (통과/전체) | 시간 |", "|---|---|---|---|---|"]
    for r in results:
        items = f"{r['passed']}/{r['passed'] + r['failed']}" if r["passed"] + r["failed"] else "결과 줄 없음"
        mark = "PASS" if r["ok"] else f"FAIL (종료 코드 {r['code']})" if r["code"] else "FAIL"
        out.append(f"| {r['num']} | {r['title']} | {mark} | {items} | {_dur(r['sec'])} |")
    return "\n".join(out)


def save_result_block(text: str) -> None:
    path = Path(__file__).resolve()
    src = path.read_text(encoding="utf-8")
    idx = src.rfind("\n" + RESULT_MARK)
    code = (src[:idx] if idx != -1 else src).rstrip()
    if "def main(" not in code:              # 안전장치: 코드가 잘려 나가는 일이 없게
        print("결과 저장을 건너뜀 (파일 구조를 확인하세요)")
        return
    body = text.replace("'''", "’’’").strip()
    stamp = time.strftime("%Y-%m-%d %H:%M")
    path.write_text(f"{code}\n\n\n{RESULT_MARK}\nr'''\n저장 {stamp}\n\n{body}\n'''\n", encoding="utf-8")
    print(f"\n결과를 이 파일 맨 아래에 저장: {path.relative_to(ROOT).as_posix()}")


def preflight() -> list[str]:
    """실행 준비 확인: docker · 이미지 · 테스트 파일. 문제 목록을 돌려준다 (빈 목록 = 준비 완료)."""
    problems = []
    try:
        if subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode != 0:
            problems.append("Docker Desktop 이 꺼져 있습니다 → 켜고 다시 실행하세요.")
        elif subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode != 0:
            problems.append(f"도커 이미지 {IMAGE} 가 없습니다 → 수정본 폴더에서 'docker compose up --build -d'")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        problems.append("docker 명령을 찾을 수 없습니다 → Docker Desktop 을 설치 · 실행하세요.")
    for f in [*(f"tests/checks_6/{n}" for n in ("1_concurrency.py", "2_redis_recovery.py", "3_packing_rules.py",
              "4_matching_ai.py", "5_ai_control.py", "6_static_checks.py", "_common.py", "_concurrency_compose.yml",
              "_celery_probe.py")), "requirements-dev.txt"]:
        if not (ROOT / f).exists():
            problems.append(f"파일 없음: {f}")
    return problems


def main(argv: list[str]) -> int:
    only = None
    if "--only" in argv:
        try:
            only = {int(x) for x in argv[argv.index("--only") + 1].split(",")}
        except (IndexError, ValueError):
            print("사용법: --only 3,4,5")
            return 2
    steps = [s for s in STEPS if only is None or s[0] in only]

    problems = preflight()
    print(f"실행 준비: 파이썬 {sys.version.split()[0]} ({sys.executable}) · 폴더 {ROOT}")
    for p in problems:
        print(f"  [FAIL] {p}")
    if "--check" in argv:
        print("  [PASS] 실행 준비 완료" if not problems else "")
        print("실행할 단계: " + " · ".join(f"{n}. {t}" for n, t, _ in steps))
        return 1 if problems else 0
    if problems:
        return 1

    t0 = time.time()
    results = [run_step(n, t, c) for n, t, c in steps]
    head = summary(results, time.time() - t0)
    print(f"\n{'=' * 78}\n{head}")
    detail = []
    for r in results:
        detail += ["", f"### {r['num']}. {r['title']}", *r["lines"]]
    save_result_block(head + "\n" + "\n".join(detail))
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
