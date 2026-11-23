"""
Redis 장애 복구 테스트 — 워커가 죽은 뒤 메시지 · 작업이 다시 처리되는지                          약 1~2분

VS Code: 이 파일을 열고 오른쪽 위 ▷ (Run Python File) 를 누르면 바로 실행된다.
  - 테스트 전용 Redis 컨테이너와 네트워크를 따로 만들어 쓰고 끝나면 지운다 → 데모 스택 · 데이터는 건드리지 않음
  - 도커 이미지(baecha-app:dev) · redis:7.4-alpine 필요 → Docker Desktop 이 켜져 있어야 함
터미널: 수정본 폴더에서  python tests/checks_6/2_redis_recovery.py

점검 내용
  A. GPS 스트림 (Redis Streams · gps_stream_worker)
     1) 백엔드가 메시지를 읽고 XACK 하기 전에 죽음 → 메시지가 '처리 중(PEL)'으로 남음
     2) 다시 시작하면 recover_pending() 이 그 메시지부터 순서대로 다시 처리 · 미처리 0건
     3) 다른 소비자(죽은 서버)가 붙잡고 있던 메시지도 가져와 처리 (XAUTOCLAIM)
     4) 스트림 길이 제한으로 이미 지워진 메시지는 '유실'로 세고 미처리 목록에서 정리
     5) 실제 워커 시작 함수 run() 이 시작하자마자 복구하는지
  B. Celery 작업 (적재 계산 워커)
     1) 작업 도중 워커 자식 프로세스 강제 종료(kill -9) → 작업이 큐로 돌아가 다시 실행 · 완료
     2) 작업 도중 워커 전체 강제 종료 → 새 워커가 visibility_timeout 뒤 작업을 다시 받아 완료

pytest · CI 의 자동 실행 목록(tests/test_suites.py)에는 넣지 않았다 (도커 컨테이너를 직접 띄우기 때문).
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]   # 수정본
IMAGE = "baecha-app:dev"
REDIS_IMAGE = "redis:7.4-alpine"
NET = "baecha-recovery-test"
REDIS_NAME = "baecha-recovery-redis"
VISIBILITY = 10   # 테스트용 Celery visibility_timeout (운영 기본 300초)


# ====================================================================== 바깥(이 PC): 테스트용 컨테이너 준비
def _docker(*args, check=False, quiet=True):
    return subprocess.run(["docker", *args], capture_output=quiet, text=True, encoding="utf-8",
                          env={**os.environ, "MSYS_NO_PATHCONV": "1"}, check=check)


def host_main() -> int:
    try:
        if _docker("image", "inspect", IMAGE).returncode != 0:
            print(f"도커 이미지 {IMAGE} 가 없습니다. 수정본 폴더에서 'docker compose up --build -d' 로 먼저 빌드하세요.")
            return 1
    except FileNotFoundError:
        print("docker 명령을 찾을 수 없습니다. Docker Desktop 을 켠 뒤 다시 실행하세요.")
        return 1
    print("테스트 전용 Redis 컨테이너 준비 중... (데모 스택과 분리)")
    _docker("rm", "-f", REDIS_NAME)
    _docker("network", "rm", NET)
    _docker("network", "create", NET, check=True)
    try:
        _docker("run", "-d", "--rm", "--name", REDIS_NAME, "--network", NET, REDIS_IMAGE, check=True)
        r = subprocess.run(
            ["docker", "run", "--rm", "--network", NET,
             "-e", f"REDIS_URL=redis://{REDIS_NAME}:6379/0", "-e", f"CELERY_VISIBILITY_TIMEOUT={VISIBILITY}",
             "-e", "PYTHONPATH=/src", "-e", "BAECHA_DB=0", "-e", "PYTHONIOENCODING=utf-8",
             "-v", f"{ROOT.as_posix()}:/src", "-w", "/src", IMAGE, "python", "tests/checks_6/2_redis_recovery.py", "--inside"],
            env={**os.environ, "MSYS_NO_PATHCONV": "1"})
        return r.returncode
    finally:
        _docker("rm", "-f", REDIS_NAME)
        _docker("network", "rm", NET)
        print("테스트 컨테이너 · 네트워크 정리 완료")


# ====================================================================== 안(도커): 실제 점검
class FakeStore:
    """apply_gps 호출만 기록하는 가짜 저장소 (실제 저장소 · DB 를 건드리지 않음)."""

    def __init__(self):
        self.seen: list[int] = []

    def apply_gps(self, msg: dict) -> bool:
        self.seen.append(int(msg["seq"]))
        return True


def part_gps():
    from _check import check, section
    from backend.workers import gps_stream_worker as gsw
    from backend.core.redis_client import get_redis

    section("A. GPS 스트림 — 읽고 XACK 전에 죽은 메시지 재처리")
    tag = uuid.uuid4().hex[:6]
    gsw.STREAM, gsw.GROUP, gsw.CONSUMER = f"test:gps:{tag}", "backend", "api-1"   # 테스트 전용 스트림
    r = get_redis()
    gsw.ensure_group()

    def add(n, start=1):
        return [r.xadd(gsw.STREAM, {"seq": str(i), "order_id": "T", "lat": "37.5", "lon": "127.0"})
                for i in range(start, start + n)]

    def pending(consumer=None):
        info = r.xpending(gsw.STREAM, gsw.GROUP)
        if consumer is None:
            return info["pending"]
        return next((int(c["pending"]) for c in info.get("consumers", []) if c["name"] == consumer), 0)

    add(10)
    gsw._read(block_ms=10, count=6)                                           # 이 서버(api-1)가 6건 읽고 죽음
    r.xreadgroup(gsw.GROUP, "api-2", {gsw.STREAM: ">"}, count=2)               # 다른 서버(api-2)가 2건 읽고 죽음
    check("죽기 전: 미처리(PEL) 8건이 Redis 에 남음 (api-1 6 · api-2 2)", pending() == 8 and pending("api-1") == 6,
          f"전체 {pending()} · api-1 {pending('api-1')} · api-2 {pending('api-2')}")
    old = gsw._read(block_ms=10, count=100)
    check("고치기 전 방식('>' 만 읽기)으로는 미처리 메시지가 다시 오지 않음",
          [int(f["seq"]) for _s, ms in old for _i, f in ms] == [9, 10], "새 메시지 9 · 10 만 옴")
    r.xack(gsw.STREAM, gsw.GROUP, *[i for _s, ms in old for i, _f in ms])

    store = FakeStore()
    n = gsw.recover_pending(store, claim_idle_ms=0)
    check("재시작 → 미처리 8건 다시 처리", n == 8 and sorted(store.seen) == list(range(1, 9)), f"처리 {n}건 {store.seen}")
    check("재처리 순서가 시간 순서 (위치가 뒤섞이지 않음)", store.seen == sorted(store.seen))
    check("재처리 후 미처리 0건 (XACK 완료)", pending() == 0, f"남은 {pending()}건")
    check("한 번 더 복구해도 중복 처리 없음", gsw.recover_pending(store, claim_idle_ms=0) == 0 and len(store.seen) == 8)

    section("A-2. 스트림 길이 제한으로 지워진 미처리 메시지")
    add(3, start=11)
    gsw._read(block_ms=10, count=3)                                           # 읽고 죽음
    r.xtrim(gsw.STREAM, maxlen=0)                                             # 그 사이 스트림이 잘림
    lost0 = gsw.stats["lost"]
    store2 = FakeStore()
    gsw.recover_pending(store2, claim_idle_ms=0)
    check("지워진 메시지는 '유실'로 세고 미처리 목록에서 정리", gsw.stats["lost"] - lost0 == 3 and pending() == 0,
          f"유실 {gsw.stats['lost'] - lost0}건 · 남은 미처리 {pending()}건")

    section("A-3. 실제 시작 함수 run() — 시작하자마자 복구 후 새 메시지 처리")
    add(4, start=21)
    gsw._read(block_ms=10, count=4)                                           # 읽고 죽음
    store3 = FakeStore()

    async def boot():
        task = asyncio.create_task(gsw.run(store3))
        await asyncio.sleep(1.0)
        await asyncio.to_thread(add, 2, 25)                                   # 재시작 뒤 새 메시지
        await asyncio.sleep(2.0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(boot())
    check("run() 시작 시 미처리 4건 복구 → 이어서 새 메시지 2건 처리", store3.seen == [21, 22, 23, 24, 25, 26],
          f"{store3.seen}")
    check("모두 XACK (미처리 0건)", pending() == 0)
    r.delete(gsw.STREAM)


def _children(pid: int) -> list[int]:
    out = []
    for p in Path("/proc").iterdir():
        if p.name.isdigit():
            try:
                if int((p / "stat").read_text().rsplit(")", 1)[1].split()[1]) == pid:
                    out.append(int(p.name))
            except (OSError, ValueError, IndexError):
                pass
    return out


def _start_worker(name: str) -> subprocess.Popen:
    return subprocess.Popen(
        ["celery", "-A", "backend.workers.celery_app", "worker", "-Q", "test_recovery", "-I", "tests.checks_6._celery_probe",
         "-c", "1", "-n", f"{name}@%h", "-l", "error", "--without-gossip", "--without-mingle", "--without-heartbeat"],
        cwd="/src", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def _wait_lines(path: Path, n: int, timeout: float) -> list[str]:
    end = time.time() + timeout
    while time.time() < end:
        lines = path.read_text().split() if path.exists() else []
        if len(lines) >= n:
            return lines
        time.sleep(0.3)
    return path.read_text().split() if path.exists() else []


def part_celery():
    from _check import check, section
    from backend.workers.celery_app import celery_app

    conf = celery_app.conf
    section("B. Celery — 작업 도중 워커가 죽어도 작업이 사라지지 않는지")
    check("설정: 완료 후 ACK(task_acks_late) · 워커 종료 시 반환(task_reject_on_worker_lost)",
          conf.task_acks_late is True and conf.task_reject_on_worker_lost is True)
    check(f"설정: visibility_timeout 테스트값 {VISIBILITY}초 (운영 기본 300초 > 작업 최대 120초)",
          conf.broker_transport_options.get("visibility_timeout") == VISIBILITY)

    w1 = _start_worker("w1")
    try:
        section("B-1. 작업 도중 워커 자식 프로세스 강제 종료 (kill -9)")
        m1 = Path(f"/tmp/recovery_{uuid.uuid4().hex[:6]}")
        res = celery_app.send_task("test.recovery_slow", args=[str(m1), 4], queue="test_recovery")
        first = _wait_lines(m1, 1, 60)
        check("작업 시작됨", len(first) >= 1, f"첫 실행 프로세스 {first[:1]}")
        if first:
            os.kill(int(first[0]), signal.SIGKILL)
        try:
            out = res.get(timeout=60)
        except Exception as e:  # noqa: BLE001
            out = {"error": f"{type(e).__name__}: {e}"}
        lines = _wait_lines(m1, 2, 1)
        check("다시 실행되어 완료 (작업 유실 없음)", out.get("done") is True and len(lines) == 2,
              f"배달 {len(lines)}회 · 결과 {out}")

        section("B-2. 작업 도중 워커 전체 강제 종료 → 새 워커가 이어받음")
        m2 = Path(f"/tmp/recovery_{uuid.uuid4().hex[:6]}")
        res2 = celery_app.send_task("test.recovery_slow", args=[str(m2), 4], queue="test_recovery")
        first = _wait_lines(m2, 1, 60)
        check("작업 시작됨", len(first) >= 1)
        os.killpg(w1.pid, signal.SIGKILL)                                     # 본체 + 자식 모두
        w1.wait(timeout=10)
        t0 = time.time()
        w2 = _start_worker("w2")
        try:
            try:
                out2 = res2.get(timeout=VISIBILITY + 60)
            except Exception as e:  # noqa: BLE001
                out2 = {"error": f"{type(e).__name__}: {e}"}
            lines2 = _wait_lines(m2, 2, 1)
            check("새 워커가 작업을 다시 받아 완료 (작업 유실 없음)",
                  out2.get("done") is True and len(lines2) == 2 and lines2[0] != lines2[1],
                  f"배달 {len(lines2)}회 · 새 워커 시작 후 {time.time() - t0:.0f}초 만에 완료")
        finally:
            os.killpg(w2.pid, signal.SIGKILL)
    finally:
        if w1.poll() is None:
            os.killpg(w1.pid, signal.SIGKILL)


def inside_main() -> int:
    sys.path.insert(0, str(ROOT / "tests"))
    sys.path.insert(0, str(ROOT))
    from _check import finish
    from backend.core.redis_client import available
    if not available():
        print("Redis 에 연결할 수 없습니다 (REDIS_URL 확인).")
        return 1
    part_gps()
    part_celery()
    try:
        finish()
    except SystemExit as e:
        # Celery 결과 구독 정리가 파이썬 종료 중에 돌며 의미 없는 오류를 찍지 않게 바로 종료
        sys.stdout.flush()
        os._exit(int(e.code or 0))
    return 0


if __name__ == "__main__":
    sys.exit(inside_main() if "--inside" in sys.argv else host_main())
