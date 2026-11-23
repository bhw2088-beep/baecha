"""테스트 스크립트 공용: 간단한 PASS/FAIL 집계 (pytest 없이 실행).

프로젝트 라이브러리(pydantic · fastapi · numpy)가 없는 파이썬으로 실행하면 (예: VS Code 가 컴퓨터 기본 파이썬을 고름)
같은 스크립트를 도커 이미지 baecha-app:dev 안에서 대신 실행하고 결과를 그대로 보여 준다 (네트워크 끔 · 외부 API 호출 없음).
테스트 파일은 모두 라이브러리보다 먼저 이 파일을 import 하므로, 라이브러리 import 오류 전에 도커로 넘어간다.
"""

import os
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)  # 수정본 폴더 → backend 패키지를 import 할 수 있게

IMAGE = "baecha-app:dev"
NEEDED = ("pydantic", "fastapi", "numpy")
# 호스트에서 도커 · 서버를 직접 다루는 스크립트는 도커로 옮기지 않는다 (동시성 · Redis 복구 · 실행 중 서버 점검)
HOST_SCRIPTS = {"1_concurrency.py", "2_redis_recovery.py", "smoke_test.py"}


def _missing_libs() -> list[str]:
    import importlib.util
    return [m for m in NEEDED if importlib.util.find_spec(m) is None]


def _rerun_in_docker(missing: list[str]) -> int:
    """지금 실행 중인 테스트 스크립트를 도커 이미지 안에서 다시 실행하고 종료 코드를 돌려준다."""
    script = os.path.abspath(sys.argv[0])
    rel = os.path.relpath(script, ROOT).replace("\\", "/")
    print(f"(이 파이썬에 {', '.join(missing)} 가 없어서 도커 이미지 {IMAGE} 안에서 실행합니다 · 네트워크 끔)", flush=True)
    try:
        if subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode != 0:
            print(f"도커 이미지 {IMAGE} 가 없습니다. 수정본 폴더에서 'docker compose up --build -d' 로 먼저 빌드하세요.")
            return 2
    except FileNotFoundError:
        print("docker 도 없습니다. Docker Desktop 을 켜거나, 프로젝트 라이브러리가 설치된 파이썬(.venv)으로 실행하세요.")
        return 2
    cmd = ["docker", "run", "--rm", "--network", "none", "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "PYTHONUNBUFFERED=1",
           "-e", "BAECHA_DB=0", "-v", f"{ROOT}:/src", "-w", "/src", IMAGE, "python", rel, *sys.argv[1:]]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for raw in proc.stdout:                       # 줄마다 바로 보여 줌 (한글은 UTF-8 그대로)
        if sys.stdout.isatty():
            print(raw.decode("utf-8", "replace"), end="", flush=True)
        else:
            sys.stdout.buffer.write(raw)
            sys.stdout.buffer.flush()
    return proc.wait()


def _maybe_docker():
    script = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if (not script.startswith(ROOT) or os.path.basename(script) in HOST_SCRIPTS
            or os.path.exists("/.dockerenv")):   # 이미 도커 안 · 테스트 스크립트가 아님 → 그대로
        return
    missing = _missing_libs()
    if missing:
        sys.exit(_rerun_in_docker(missing))


_maybe_docker()

os.environ.setdefault("BAECHA_DB", "0")  # 테스트는 메모리 저장소만 사용 (실제 DB 데이터를 건드리지 않음)
# 테스트는 항상 모의 판정 · 규칙 기반으로 (.env 에 실제 국세청 · Gemini 키가 있어도 외부 API 를 부르지 않음)
os.environ["NTS_API_KEY"] = ""
os.environ["GEMINI_API_KEY"] = ""

_result = {"pass": 0, "fail": 0}


def check(name: str, ok: bool, detail: str = ""):
    _result["pass" if ok else "fail"] += 1
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f"  — {detail}" if detail else ""))


def section(title: str):
    print(f"\n■ {title}")


def finish():
    p, f = _result["pass"], _result["fail"]
    print(f"\n결과: {p}개 통과 · {f}개 실패")
    sys.exit(1 if f else 0)
