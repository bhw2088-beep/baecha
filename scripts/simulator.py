"""
배차의 민족 — 통합 시뮬레이터 (1단계 스모크 · 2단계 효율성 검증)

순서는 항상  [기사 먼저 생성] → [주문 먼저 생성] → [매칭 · 수락 · 운행] → [GPS 핑 전송] → [측정 결과]

  1. 스모크 테스트   기사 5명 · 주문 1건 (서울 → 부산 2.5톤)
                     가입 검증(노란 번호판 · 사업자번호) · 주문 사전 검증 · 매칭 · 수락 · 운행 · GPS 핑 · 재위탁 차단이
                     예외 없이 정확히 통과 / 실패 판정을 내리는지 체크리스트로 확인
  2. 효율성 검증     기사 1,000명(거점 7곳 주변에 분산) · 주문 100건을 한꺼번에
                     → PostGIS 반경 쿼리(ST_DWithin) + LightGBM + 헝가리안 배치가 3~5초 주기 안에 소화되는지,
                       ThreadPoolExecutor 로 GPS 핑을 초당 수백 건 보내 Redis Streams 가 유실 없이 처리하는지,
                       그동안 백엔드 · DB · Redis 의 CPU · 메모리가 어떻게 변하는지 측정

실행 (수정본 폴더에서 · Docker Desktop 이 켜져 있어야 함 · 도커 이미지 baecha-app:dev 필요)
    python scripts/simulator.py              # 번호 메뉴 (VS Code 에서 이 파일을 열고 ▷ 를 눌러도 같음)
    python scripts/simulator.py --mode 1     # 스모크 바로 실행
    python scripts/simulator.py --mode 2 --drivers 500 --orders 100 --rps 300 --gps-seconds 60
    python scripts/simulator.py --mode 2 --keep   # 끝나도 테스트 스택을 지우지 않음 (직접 들여다볼 때)

테스트는 시연용 서버(8000 · 8501)와 분리된 '부하 테스트 전용 스택'(scripts/docker-compose.loadtest.yml ·
API 127.0.0.1:18000)에서 한다. 실행할 때마다 새로 만들고, 끝나면 지운다 (--keep 이면 남김).
→ 시연 데이터 · DB 는 건드리지 않는다. 외부 API(국세청 · Gemini)도 부르지 않는다.

결과: 터미널 표 + scripts/results/mode<N>_latest.json + 이 파일 맨 아래 ''' ''' 블록 (문자열이라 실행되지 않음)
필요한 것: 파이썬 3.10+ · requests (pip install requests)
"""

from __future__ import annotations

import argparse                      # 명령행 옵션 (--mode 등)
import itertools                     # GPS 핑 순번 카운터
import json                          # 결과 저장
import random                        # 무작위 기사 · 주문 · 수락
import re                            # 시드 파일에서 데모 비밀번호 읽기, docker stats 파싱
import statistics                    # 평균 · 중앙값
import subprocess                    # docker 명령 실행 (테스트 스택 켜기 · 끄기 · 자원 측정)
import sys
import threading                     # 자원 측정 스레드 · 스레드별 HTTP 세션
import time                          # 시간 측정
from concurrent.futures import ThreadPoolExecutor   # 동시 요청 (가입 · 주문 · GPS 핑)
from pathlib import Path

try:
    import requests                  # HTTP 요청 (서버 API 호출)
    from requests.adapters import HTTPAdapter
except ImportError:                  # requests 가 없으면 설치 방법만 알려 주고 끝낸다
    print("requests 가 필요합니다. 터미널에서:  pip install requests")
    sys.exit(1)

if hasattr(sys.stdout, "reconfigure"):    # 윈도우 콘솔에서 한글 · 기호가 깨지지 않게
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ======================================================================= 기본 설정
ROOT = Path(__file__).resolve().parent.parent                 # 수정본 폴더
RESULTS = Path(__file__).resolve().parent / "results"          # 결과 JSON 저장 폴더
COMPOSE_FILE = Path(__file__).resolve().parent / "docker-compose.loadtest.yml"   # 테스트 전용 스택
PROJECT = "baecha-load"                                        # 테스트 스택의 도커 프로젝트 이름
IMAGE = "baecha-app:dev"                                       # 시연 스택이 빌드한 앱 이미지
# 테스트 스택 API 주소 (이 PC 에서만). localhost 가 아니라 127.0.0.1 로 — 윈도우는 localhost 를 IPv6(::1) 로 먼저 시도하는데
# 테스트 스택은 127.0.0.1 에만 열려 있어서, 새 연결마다 ::1 거절을 2초 기다린 뒤 127.0.0.1 로 붙는다 (지연 2초 튐의 원인)
BASE_URL = "http://127.0.0.1:18000/api/v1"
CONTAINERS = {"backend": f"{PROJECT}-backend-1", "db": f"{PROJECT}-db-1", "redis": f"{PROJECT}-redis-1"}
TIMEOUT = 30                                                   # 요청 하나를 기다리는 최대 시간(초)

ADMIN_EMAIL = "admin.control@baecha.co.kr"      # 시드 데이터의 관제 계정 (테스트 스택에도 똑같이 생김)
SHIPPER_EMAIL = "logistics@samsung.com"         # 시드 데이터의 화주 계정
DRIVER_PASSWORD = "loadtest-pass-1234"          # 이 시뮬레이터가 만드는 가상 기사들의 비밀번호 (테스트 스택 전용)

# 대한민국 주요 물류 거점 → 그 주변 '지역 이름' (가입 · 주문 API 는 위경도가 아니라 지역 이름을 받는다)
# 거점 좌표 · 비중은 서버의 /meta 에서 받고, 여기서는 거점마다 어떤 지역들이 주변인지만 정한다.
HUB_REGIONS = {
    "서울/인천": ["서울", "인천", "경기 파주"],
    "평택항": ["경기 평택", "경기 안성", "경기 화성", "충남 아산", "충남 당진"],
    "대전 HUB": ["대전", "충북 청주"],
    "대구": ["대구", "경북 구미"],
    "광주": ["광주"],
    "울산": ["울산"],
    "부산항": ["부산"],
}
# 기사 차종 분포 (비중) — 냉동차는 적게
TRUCK_MIX = {"1톤 카고": 0.15, "2.5톤 카고": 0.25, "5톤 윙바디": 0.25, "5톤 냉동탑차": 0.05,
             "11톤 탑차": 0.15, "11톤 냉동탑차": 0.05, "25톤 무진동": 0.10}
PLATE_AREAS = ["서울", "경기", "인천", "부산", "대구", "광주", "대전", "울산", "충북", "충남", "경북", "강원"]
BIZ_WEIGHTS = (1, 3, 7, 1, 3, 7, 1, 3, 5)       # 사업자등록번호 Modulus-10 가중치 (서버와 같은 규칙)


def log(msg: str) -> None:
    """시각을 붙여 진행 상황을 출력."""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def pct(values: list[float], p: float) -> float | None:
    """백분위수 (p95 등). 값이 없으면 None."""
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))]


def ms_summary(values: list[float]) -> dict:
    """지연 시간 목록(ms) → 건수 · 평균 · p50 · p95 · p99 · 최대."""
    if not values:
        return {"count": 0}
    return {"count": len(values), "avg_ms": round(statistics.mean(values), 1), "p50_ms": round(pct(values, 50), 1),
            "p95_ms": round(pct(values, 95), 1), "p99_ms": round(pct(values, 99), 1), "max_ms": round(max(values), 1)}


# ======================================================================= HTTP (스레드마다 세션 하나)
_local = threading.local()


def _session() -> requests.Session:
    """스레드별 HTTP 세션 (연결 재사용 → 부하 생성기 자체가 병목이 되지 않게)."""
    s = getattr(_local, "s", None)
    if s is None:
        s = requests.Session()
        s.mount("http://", HTTPAdapter(pool_connections=4, pool_maxsize=16))
        _local.s = s
    return s


def call(method: str, path: str, token: str | None = None, **kw) -> tuple[int, object, float]:
    """API 호출 → (상태 코드, 응답 JSON 또는 오류 문구, 걸린 시간 ms). 연결 실패 · 시간 초과는 상태 0."""
    headers = kw.pop("headers", {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    t0 = time.perf_counter()
    try:
        r = _session().request(method, BASE_URL + path, headers=headers, timeout=kw.pop("timeout", TIMEOUT), **kw)
        ms = (time.perf_counter() - t0) * 1000
        try:
            body = r.json()
        except ValueError:
            body = r.text[:200]
        return r.status_code, body, ms
    except requests.Timeout:
        return 0, "시간 초과", (time.perf_counter() - t0) * 1000
    except requests.RequestException as e:            # 연결 거부 · 끊김 등
        return 0, f"연결 오류: {type(e).__name__}", (time.perf_counter() - t0) * 1000


def detail(body: object) -> str:
    """오류 응답에서 사람이 읽을 문구만."""
    return body.get("detail", str(body))[:120] if isinstance(body, dict) else str(body)[:120]


# ======================================================================= 테스트 스택 (도커)
def docker(*args: str, check: bool = False, timeout: int = 300) -> subprocess.CompletedProcess:
    """docker 명령 실행 (출력은 모아서 돌려줌)."""
    return subprocess.run(["docker", *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                          check=check, timeout=timeout)


def compose(*args: str, timeout: int = 300) -> subprocess.CompletedProcess:
    """테스트 전용 스택에 docker compose 명령."""
    return docker("compose", "-f", str(COMPOSE_FILE), *args, timeout=timeout)


def check_docker() -> bool:
    """도커가 켜져 있고 앱 이미지가 있는지 확인. 아니면 할 일을 알려 주고 False."""
    try:
        if docker("info", timeout=30).returncode != 0:
            log("도커가 실행 중이 아닙니다. Docker Desktop 을 켠 뒤 다시 실행하세요.")
            return False
    except (FileNotFoundError, subprocess.TimeoutExpired):
        log("docker 명령을 찾을 수 없습니다. Docker Desktop 을 설치 · 실행하세요.")
        return False
    if docker("image", "inspect", IMAGE, timeout=30).returncode != 0:
        log(f"도커 이미지 {IMAGE} 가 없습니다. 수정본 폴더에서 'docker compose up --build -d' 로 먼저 빌드하세요.")
        return False
    return True


def wait_health(seconds: int = 180) -> bool:
    """API 가 응답할 때까지 기다림 (서버 시작 · 시드 데이터 생성 시간)."""
    end = time.time() + seconds
    while time.time() < end:
        code, _, _ = call("GET", "/health", timeout=5)
        if code == 200:
            return True
        time.sleep(2)
    return False


def stack_up() -> bool:
    """테스트 스택을 깨끗한 상태로 새로 켠다 (이전 테스트 데이터 삭제 → 결과를 서로 비교할 수 있게)."""
    log("테스트 전용 스택 준비 (이전 테스트 데이터 삭제 후 새로 시작) ...")
    compose("down", "-v", "--remove-orphans", timeout=180)
    r = compose("up", "-d", timeout=300)
    if r.returncode != 0:
        log("테스트 스택을 켜지 못했습니다:\n" + (r.stderr or r.stdout)[-800:])
        return False
    if not wait_health():
        log("API 가 응답하지 않습니다. 로그: docker compose -f scripts/docker-compose.loadtest.yml logs backend")
        return False
    log(f"테스트 스택 준비 완료 · API {BASE_URL}")
    return True


def stack_down(keep: bool) -> None:
    """테스트 스택 정리 (--keep 이면 남겨 둔다)."""
    if keep:
        log("테스트 스택을 남겨 둡니다 (--keep). 지우려면: docker compose -f scripts/docker-compose.loadtest.yml down -v")
        return
    compose("down", "-v", "--remove-orphans", timeout=180)
    log("테스트 스택 삭제 완료 (시연 스택은 그대로)")


# ======================================================================= 자원 측정 (docker stats)
class ResourceMonitor(threading.Thread):
    """2초마다 백엔드 · DB · Redis 컨테이너의 CPU · 메모리를 기록 (단계 이름을 함께 남김)."""

    def __init__(self):
        super().__init__(daemon=True)
        self.timeline: list[tuple[float, str]] = []   # (시작 시각, 단계 이름)
        self.phase = "준비"                       # 지금 진행 중인 단계 (가입 · 주문 · 매칭 · GPS)
        self.samples: list[dict] = []
        self._halt = threading.Event()

    @property
    def phase(self) -> str:
        return self._phase

    @phase.setter
    def phase(self, name: str) -> None:
        self._phase = name
        self.timeline.append((time.time(), name))

    def phase_at(self, t: float) -> str:
        """그 시각에 진행 중이던 단계 이름."""
        name = self.timeline[0][1] if self.timeline else "-"
        for start, n in self.timeline:
            if start <= t:
                name = n
        return name

    @staticmethod
    def _mib(text: str) -> float:
        """'123.4MiB' · '1.2GiB' · '800kB' → MiB."""
        m = re.match(r"([\d.]+)\s*([KMG]i?B|B|kB)", text.strip())
        if not m:
            return 0.0
        v, unit = float(m.group(1)), m.group(2).upper()
        return v * {"B": 1 / 1048576, "KB": 1 / 1024, "KIB": 1 / 1024, "MB": 1, "MIB": 1, "GB": 1024, "GIB": 1024}[unit]

    def run(self):
        while not self._halt.is_set():
            try:
                r = docker("stats", "--no-stream", "--format", "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}",
                           *CONTAINERS.values(), timeout=20)
                now = time.time()
                for line in r.stdout.splitlines():
                    name, cpu, mem = (line.split("|") + ["", ""])[:3]
                    role = next((k for k, v in CONTAINERS.items() if v == name.strip()), None)
                    if role:
                        self.samples.append({"t": now, "phase": self.phase, "role": role,
                                             "cpu": float(cpu.strip().rstrip("%") or 0),
                                             "mem_mib": round(self._mib(mem.split("/")[0]), 1)})
            except (subprocess.TimeoutExpired, ValueError):
                pass
            self._halt.wait(2)

    def stop(self):
        self._halt.set()

    def summary(self) -> dict:
        """컨테이너별 CPU 최대 · 평균, 메모리 시작 · 최대 · 끝 (끝 − 시작 = 증가량 → 누수 의심 지표)."""
        out = {}
        for role in CONTAINERS:
            xs = [s for s in self.samples if s["role"] == role]
            if not xs:
                continue
            out[role] = {"cpu_max": round(max(s["cpu"] for s in xs), 1),
                         "cpu_avg": round(statistics.mean(s["cpu"] for s in xs), 1),
                         "mem_start_mib": xs[0]["mem_mib"], "mem_max_mib": max(s["mem_mib"] for s in xs),
                         "mem_end_mib": xs[-1]["mem_mib"],
                         "cpu_max_by_phase": {p: round(max(s["cpu"] for s in xs if s["phase"] == p), 1)
                                              for p in dict.fromkeys(s["phase"] for s in xs)}}
        return out


def redis_stream_state() -> dict:
    """Redis Streams 상태: 스트림 길이(XLEN) · 소비자 그룹 미처리(pending) · 아직 안 읽은 메시지(lag)."""
    out = {}
    r = docker("exec", CONTAINERS["redis"], "redis-cli", "XLEN", "gps:stream", timeout=20)
    if r.returncode == 0 and r.stdout.strip().isdigit():
        out["xlen"] = int(r.stdout.strip())
    r = docker("exec", CONTAINERS["redis"], "redis-cli", "XINFO", "GROUPS", "gps:stream", timeout=20)
    lines = [x.strip() for x in r.stdout.splitlines()]
    for key in ("pending", "lag"):                      # redis-cli 출력: 키 다음 줄이 값
        if key in lines:
            v = lines[lines.index(key) + 1]
            out[key] = int(v) if v.lstrip("-").isdigit() else None
    return out


# ======================================================================= 테스트 데이터 만들기
def demo_password() -> str:
    """시드 데이터의 데모 계정 비밀번호 (backend/core/seed.py · 테스트 스택에서만 사용, 화면에 출력하지 않음)."""
    src = (ROOT / "backend" / "core" / "seed.py").read_text(encoding="utf-8")
    return re.search(r'DEMO_PASSWORD\s*=\s*"([^"]+)"', src).group(1)


def seed_driver_emails() -> list[str]:
    """시드 데이터에 들어 있는 데모 기사 계정 (이 기사들도 제안을 받을 수 있어서 함께 로그인해 둔다)."""
    src = (ROOT / "backend" / "core" / "seed.py").read_text(encoding="utf-8")
    return re.findall(r'"email":\s*"([^"]+)",\s*"role":\s*"driver"', src)


def biz_no(first9: int) -> str:
    """앞 9자리 → Modulus-10 검증 숫자를 붙인 사업자등록번호 (서버 compliance_engine 과 같은 계산)."""
    d = [int(c) for c in f"{first9:09d}"]
    s = sum(x * w for x, w in zip(d, BIZ_WEIGHTS)) + (d[8] * 5) // 10
    return f"{first9:09d}{(10 - s % 10) % 10}"


def plate(n: int) -> str:
    """서로 다른 사업용 노란 번호판 (용도 문자 바 · 사 · 아 · 자). 예: '경기 88바 7712'."""
    return (f"{PLATE_AREAS[(n // 1_200_000) % len(PLATE_AREAS)]} {70 + (n // 40_000) % 30}"
            f"{'바사아자'[(n // 10_000) % 4]} {n % 10_000:04d}")


class World:
    """서버에서 받은 지역 · 거점 정보와 이번 실행에서 만든 계정 · 주문 · 운행을 한곳에 모은다."""

    def __init__(self, seed: int):
        self.rng = random.Random(seed)                  # 같은 시드면 같은 기사 · 주문 구성
        self.run_id = time.strftime("%m%d%H%M%S")       # 이메일이 겹치지 않게 실행마다 다른 표시
        self.base = self.rng.randrange(0, 900_000)      # 번호판 · 전화번호 · 사업자번호 시작 위치
        self.regions: list[str] = []
        self.hubs: list[dict] = []
        self.admin = self.shipper = None                # 관제 · 화주 토큰
        self.drivers: dict[str, dict] = {}              # driver_id → {token, truck_type, location}
        self.orders: dict[str, dict] = {}               # order_id → {created_at, first_offer_at, assigned_at, ...}
        self.runs: list[dict] = []                      # 운행 중 (GPS 핑 대상): order_id · token · lat · lon
        self.round_log: RoundCollector | None = None    # 서버 매칭 라운드 수집 (주문 생성 직전에 시작)
        self.orders_done = threading.Event()            # 주문 생성이 끝났다는 신호 (매칭 감시가 이걸 보고 끝냄)
        self.lock = threading.Lock()                    # 여러 스레드가 함께 쓰는 기록 · 난수 보호

    def order_entry(self, oid: str) -> dict:
        """주문 기록 (매칭 감시 스레드가 생성 응답보다 먼저 제안을 볼 수도 있어서, 먼저 본 쪽이 만든다)."""
        with self.lock:
            return self.orders.setdefault(oid, {"created_at": None, "first_offer_at": None, "assigned_at": None,
                                                "offers": 0, "declines": 0, "truck_type": None, "route": None})

    def load_meta(self) -> None:
        """서버 /meta 에서 지역 목록 · 거점(좌표 · 비중)을 받는다."""
        code, body, _ = call("GET", "/meta")
        if code != 200:
            raise RuntimeError(f"/meta 실패: {detail(body)}")
        self.regions, self.hubs = body["regions"], body["hubs"]

    def region_near(self, hub: str) -> str:
        """거점 주변 지역 이름 하나 (HUB_REGIONS 의 접두어로 고름)."""
        pool = [r for r in self.regions if any(r.startswith(p) for p in HUB_REGIONS.get(hub, []))]
        return self.rng.choice(pool or self.regions)

    def pick_hub(self, exclude: str | None = None) -> str:
        """거점 하나를 비중대로 (서울/인천 36% · 평택항 16% ...)."""
        hubs = [h for h in self.hubs if h["name"] != exclude]
        return self.rng.choices([h["name"] for h in hubs], weights=[h["weight"] or 0.1 for h in hubs])[0]


# ======================================================================= 1) 기사 먼저 생성
DOC_FILES = {"license_photo": ("license.jpg", b"\xff\xd8\xff loadtest license", "image/jpeg"),
             "cargo_license_photo": ("cargo_license.jpg", b"\xff\xd8\xff loadtest cargo", "image/jpeg"),
             "vehicle_photo": ("vehicle.jpg", b"\xff\xd8\xff loadtest vehicle", "image/jpeg")}


def register_driver(w: World, i: int, location: str, truck_type: str, *, plate_no: str | None = None,
                    biz: str | None = None) -> tuple[int, object, float]:
    """가상 기사 한 명 가입 (POST /auth/register, 실제 화면과 같은 multipart 폼 + 서류 3종)."""
    n = w.base + i
    form = {"role": "driver", "email": f"lt{w.run_id}-d{i}@loadtest.local", "password": DRIVER_PASSWORD,
            "name": f"부하기사{i:04d}", "phone": f"010-{(20_000_000 + n) // 10_000 % 10_000:04d}-{n % 10_000:04d}",
            "biz_no": biz or biz_no(100_000_000 + n * 7), "plate": plate_no or plate(n),
            "truck_type": truck_type, "base_location": location}
    return call("POST", "/auth/register", data=form, files=DOC_FILES)


def seed_drivers(w: World, count: int, *, hub: str | None = None, trucks: list[str] | None = None,
                 workers: int = 8) -> dict:
    """기사 count 명을 가입 → 관제 승인 (노란 번호판 · 승인된 기사만 매칭 후보가 된다).
    hub 를 주면 그 거점 주변에만, 아니면 거점 비중대로 전국에 분산."""
    plan = []
    for i in range(count):
        h = hub or w.pick_hub()
        truck = trucks[i % len(trucks)] if trucks else w.rng.choices(list(TRUCK_MIX), weights=list(TRUCK_MIX.values()))[0]
        plan.append((i + 1, w.region_near(h), truck))
    reg_ms, fails, ids = [], [], []

    def one(args):
        i, loc, truck = args
        code, body, ms = register_driver(w, i, loc, truck)
        return i, loc, truck, code, body, ms

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:                  # 가입은 여러 스레드로 동시에
        for n, (i, loc, truck, code, body, ms) in enumerate(ex.map(one, plan), 1):
            reg_ms.append(ms)
            if code == 200 and isinstance(body, dict):
                did = body["user"]["org_id"]
                w.drivers[did] = {"token": body["token"], "truck_type": truck, "location": loc}
                ids.append(did)
            else:
                fails.append(f"{code} {detail(body)}")
            if n % max(1, count // 10) == 0 or n == count:
                log(f"  기사 가입 {n:,}/{count:,} (실패 {len(fails)})")
    reg_sec = time.time() - t0

    approve_ms, approve_fail = [], 0                                        # 관제 승인 (가입 심사)

    def approve(did):
        return call("POST", f"/admin/compliance/drivers/{did}", w.admin, json={"approve": True})

    t1 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for code, body, ms in ex.map(approve, ids):
            approve_ms.append(ms)
            approve_fail += code != 200
    log(f"  기사 승인 {len(ids) - approve_fail:,}/{len(ids):,}")
    return {"requested": count, "registered": len(ids), "failed": len(fails), "fail_reasons": fails[:5],
            "register_sec": round(reg_sec, 1), "register_per_sec": round(len(ids) / reg_sec, 1) if reg_sec else None,
            "register_latency": ms_summary(reg_ms), "approved": len(ids) - approve_fail,
            "approve_sec": round(time.time() - t1, 1), "approve_latency": ms_summary(approve_ms)}


def login_seed_drivers(w: World, password: str) -> int:
    """시드 데모 기사들도 로그인 (제안이 이 기사들에게 가도 수락 · 거절할 수 있게)."""
    n = 0
    for email in seed_driver_emails():
        code, body, _ = call("POST", "/auth/login", json={"email": email, "password": password})
        if code == 200:
            w.drivers.setdefault(body["user"]["org_id"], {"token": body["token"], "truck_type": None, "location": None})
            n += 1
    return n


# ======================================================================= 2) 주문 먼저 생성
def price_for(w: World, truck: str, origin: str, dest: str) -> int:
    """AI 적정 운임(POST /pricing/estimate) 근처 가격 — 너무 싸면 매칭 사유에 '운임 낮음'이 붙는다."""
    code, body, _ = call("POST", "/pricing/estimate", w.shipper, json={"truck_type": truck, "origin": origin,
                                                                        "destination": dest})
    rec = body.get("recommended") if code == 200 and isinstance(body, dict) else None
    return int((rec or 300_000) * w.rng.uniform(1.0, 1.1) / 5000) * 5000


def seed_orders(w: World, count: int, *, fixed: dict | None = None, workers: int = 20) -> dict:
    """화주 토큰으로 주문 count 건을 한꺼번에 생성 (POST /api/v1/shipper/orders · 서버 사전 검증 통과 확인).
    fixed 를 주면 그 내용으로 (스모크: 서울 → 부산 2.5톤)."""
    plan = []
    for i in range(count):
        if fixed:
            o = dict(fixed)
        else:
            a = w.pick_hub()
            b = w.pick_hub(exclude=a)
            truck = w.rng.choices(list(TRUCK_MIX), weights=list(TRUCK_MIX.values()))[0]
            o = {"truck_type": truck, "origin": w.region_near(a), "destination": w.region_near(b),
                 "cold_chain": "냉동" in truck}
        o["title"] = f"[부하 테스트] {i + 1:04d}"
        o["price"] = price_for(w, o["truck_type"], o["origin"], o["destination"])   # 가격은 미리 (측정에서 제외)
        plan.append(o)
    lat, fails = [], []

    done_at: dict[str, float] = {}

    def one(o):
        code, body, ms = call("POST", "/shipper/orders", w.shipper, json=o)
        if code == 201 and isinstance(body, dict):
            done_at[body["id"]] = time.time()          # 서버가 주문을 접수한 시각 (응답 받은 순간)
        return o, code, body, ms

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:                  # '동시 다발 인입' → 한꺼번에
        for o, code, body, ms in ex.map(one, plan):
            lat.append(ms)
            if code == 201 and isinstance(body, dict):
                e = w.order_entry(body["id"])
                e.update(created_at=done_at[body["id"]], truck_type=o["truck_type"],
                         route=f"{o['origin']} → {o['destination']}")
            else:
                fails.append(f"{code} {detail(body)}")
    sec = time.time() - t0
    w.orders_done.set()                                                  # 매칭 감시 스레드에 '주문 다 들어옴' 알림
    log(f"  주문 생성 {count - len(fails):,}/{count:,} (실패 {len(fails)}) · {sec:.1f}초")
    return {"requested": count, "created": count - len(fails), "failed": len(fails), "fail_reasons": fails[:5],
            "sec": round(sec, 2), "latency": ms_summary(lat)}


# ======================================================================= 3) 매칭 → 수락 → 운행
class RoundCollector(threading.Thread):
    """서버가 기록하는 매칭 라운드(PostGIS 반경 → 점수 → LightGBM → 헝가리안 한 바퀴)를 모은다.
    메트릭 API 는 최근 15회만 돌려주므로 0.5초마다 받아서 id 로 쌓는다. 시작 전에 있던 라운드(시드 데이터)는 뺀다.
    주문 생성 직전에 시작해야 '주문이 들어오자마자 돈 라운드'까지 잡힌다."""

    def __init__(self, w: "World"):
        super().__init__(daemon=True)
        self.w = w
        self.rounds: dict[str, dict] = {}
        self.old: set[str] = set()
        self._halt = threading.Event()

    def _fetch(self) -> list[dict]:
        code, m, _ = call("GET", "/admin/matching/metrics", self.w.admin, timeout=10)
        return (m.get("recent") or []) if code == 200 and isinstance(m, dict) else []

    def begin(self) -> "RoundCollector":
        self.old = {e["id"] for e in self._fetch()}     # 테스트 전부터 있던 라운드
        self.start()
        return self

    def run(self):
        while not self._halt.is_set():
            for e in self._fetch():
                if e["id"] not in self.old and e.get("orders"):   # 대기 화물이 있던 라운드만
                    self.rounds[e["id"]] = e
            self._halt.wait(0.5)

    def finish(self) -> list[dict]:
        self._halt.set()
        if self.is_alive():
            self.join(timeout=15)
        for e in self._fetch():                          # 마지막으로 한 번 더
            if e["id"] not in self.old and e.get("orders"):
                self.rounds[e["id"]] = e
        return list(self.rounds.values())


def run_matching(w: World, *, accept_rate: float, limit_sec: int, workers: int = 16) -> dict:
    """관제의 '응답 대기 제안' 목록을 0.5초마다 보고, 제안받은 기사가 확률적으로 수락 · 거절 → 수락하면 운행 시작.
    (관제 API 를 부를 때마다 서버가 매칭 라운드를 돌린다 = 3~5초 주기 배치와 같은 효과)
    주문 생성 '전에' 시작해야(start_matching) 주문 접수 → 첫 제안 시간을 정확히 잰다.
    주문 생성이 끝나고(w.orders_done) 우리 주문이 모두 배차 확정되거나 limit_sec 가 지나면 끝."""
    seen: set[tuple[str, str]] = set()                  # (주문, 기사) 이미 응답한 제안
    pool = ThreadPoolExecutor(max_workers=workers)
    poll_ms = []

    def respond(oid: str, did: str, token: str, mine: bool):
        with w.lock:                                                         # 난수는 여러 스레드가 함께 쓰므로 잠금
            yes = w.rng.random() < accept_rate
        if yes:                                     # 수락
            code, body, _ = call("POST", f"/driver/offer/{oid}/accept", token)
            if code != 200:
                return
            if mine:
                w.orders[oid]["assigned_at"] = time.time()
            code, body, _ = call("POST", f"/driver/runs/{oid}/start", token)   # 상차 완료 → 운행 시작
            if code == 200 and isinstance(body, dict) and mine:
                with w.lock:
                    w.runs.append({"order_id": oid, "driver_id": did, "token": token,
                                   "lat": body.get("lat"), "lon": body.get("lon")})
        else:                                                                # 거절 → 서버가 다음 기사에게 재배정
            call("POST", f"/driver/offer/{oid}/decline", token)
            if mine:
                w.orders[oid]["declines"] += 1

    t0 = time.time()
    while time.time() - t0 < limit_sec:
        code, body, ms = call("GET", "/admin/offers", w.admin)
        poll_ms.append(ms)
        if code == 200 and isinstance(body, list):
            for v in body:
                oid = v["id"]
                did = ((v.get("matching") or {}).get("driver") or {}).get("id")
                if not did or (oid, did) in seen:
                    continue
                seen.add((oid, did))
                mine = str(v.get("title", "")).startswith("[부하 테스트]")   # 이 시뮬레이터가 만든 주문
                if mine:
                    o = w.order_entry(oid)
                    o["offers"] += 1
                    o["first_offer_at"] = o["first_offer_at"] or time.time()
                token = (w.drivers.get(did) or {}).get("token")
                if token:                                   # 우리가 모르는 기사면 응답 못 함 → 시간 초과 후 재배정
                    pool.submit(respond, oid, did, token, mine)
        if w.orders_done.is_set() and w.orders and all(o["assigned_at"] for o in w.orders.values()):
            break
        time.sleep(0.5)
    pool.shutdown(wait=True)

    # 주문 접수(생성 응답) → 첫 제안 · 배차 확정까지 (감시 주기 0.5초 단위 · 음수는 0)
    first = [max(0.0, o["first_offer_at"] - o["created_at"]) for o in w.orders.values()
             if o["first_offer_at"] and o["created_at"]]
    done = [max(0.0, o["assigned_at"] - o["created_at"]) for o in w.orders.values()
            if o["assigned_at"] and o["created_at"]]
    within5 = sum(1 for x in first if x <= 5)
    rounds = w.round_log.finish()                       # 주문 생성부터 지금까지 서버가 돌린 매칭 라운드
    round_ms = [e["latency_ms"] for e in rounds if e.get("latency_ms") is not None]
    biggest = max(rounds, key=lambda e: e.get("orders") or 0, default={})
    # 배차 못 된 주문의 사유 (관제 '매칭 실패' 목록)
    code, failed, _ = call("GET", "/admin/failed-matches", w.admin)
    reasons: dict[str, int] = {}
    if code == 200 and isinstance(failed, list):
        for v in failed:
            if v["id"] in w.orders:
                for r in v.get("reasons") or []:
                    reasons[r] = reasons.get(r, 0) + 1
    n = len(w.orders)
    return {
        "orders": n, "offered": len(first), "assigned": len(done), "unassigned": n - len(done),
        "first_offer_sec": {"p50": round(pct(first, 50), 2) if first else None,
                            "p95": round(pct(first, 95), 2) if first else None,
                            "max": round(max(first), 2) if first else None},
        "first_offer_within_5s_pct": round(100 * within5 / n, 1) if n else None,
        "assigned_sec": {"p50": round(pct(done, 50), 2) if done else None,
                         "p95": round(pct(done, 95), 2) if done else None,
                         "max": round(max(done), 2) if done else None},
        "all_assigned_sec": round(max(done), 2) if done and len(done) == n else None,
        "declines": sum(o["declines"] for o in w.orders.values()),
        "server_round_ms": ms_summary(round_ms),            # 이번 테스트 동안 주문이 있던 라운드만
        "biggest_round": {k: biggest.get(k) for k in ("orders", "drivers", "candidate_pairs", "assigned", "latency_ms")},
        "spatial_filter": "PostGIS ST_DWithin (테스트 스택 DB)",
        "admin_poll": ms_summary(poll_ms), "runs_started": len(w.runs), "fail_reasons": reasons,
    }


def start_matching(w: World, *, accept_rate: float, limit_sec: int) -> tuple[threading.Thread, dict]:
    """매칭 감시(run_matching)를 주문 생성 '전에' 백그라운드로 시작. finish_matching 으로 결과를 받는다."""
    out: dict = {}

    def target():
        try:
            out.update(run_matching(w, accept_rate=accept_rate, limit_sec=limit_sec))
        except Exception as e:  # noqa: BLE001 — 감시 스레드 오류도 결과에 남긴다
            out["error"] = f"{type(e).__name__}: {e}"

    t = threading.Thread(target=target, daemon=True)
    t.start()
    return t, out


def finish_matching(t: threading.Thread, out: dict, limit_sec: int) -> dict:
    t.join(timeout=limit_sec + 60)
    if out.get("error"):
        raise RuntimeError(f"매칭 감시 오류: {out['error']}")
    return out


# ======================================================================= 4) GPS 핑 부하
def gps_status(w: World) -> dict:
    """서버의 GPS 수신 파이프라인 카운터 (Redis Streams 소비: processed · ignored · errors)."""
    code, body, _ = call("GET", "/admin/gps", w.admin)
    return body if code == 200 and isinstance(body, dict) else {}


def gps_flood(w: World, *, rps: int, seconds: int, workers: int) -> dict:
    """ThreadPoolExecutor 로 운행 중인 기사들이 GPS 핑(POST /tracking/ping, 기사 토큰)을 초당 rps 건씩 seconds 초 동안.
    → 응답 지연 · 실패율 · 실제 처리량 · Redis Streams 유실(보낸 건수 vs 서버가 처리한 건수)."""
    runs = [r for r in w.runs if r["lat"] is not None]
    if not runs:
        return {"skipped": "운행 중인 화물이 없어 GPS 핑을 보낼 수 없음 (매칭 · 수락 · 운행 시작이 먼저)"}
    before = gps_status(w)
    counter = itertools.count()                   # 전체 핑 순번 → 보낼 시각을 정해 초당 rps 건으로 맞춘다
    start = time.perf_counter() + 0.5
    end = start + seconds
    lat_ms: list[float] = []
    codes: dict[str, int] = {}
    queued = 0
    lock = threading.Lock()

    def worker():
        nonlocal queued
        rng = random.Random()
        while True:
            k = next(counter)
            at = start + k / rps                  # k 번째 핑을 보낼 시각
            if at >= end:
                return
            delay = at - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            r = rng.choice(runs)                  # 운행 중인 기사 하나
            body = {"order_id": r["order_id"], "lat": round(r["lat"] + rng.gauss(0, 0.0001), 6),   # 약 10m 흔들림
                    "lon": round(r["lon"] + rng.gauss(0, 0.0001), 6), "speed": rng.randint(60, 90)}
            code, resp, ms = call("POST", "/tracking/ping", r["token"], json=body, timeout=10)
            with lock:
                lat_ms.append(ms)
                codes[str(code)] = codes.get(str(code), 0) + 1
                if code == 202 and isinstance(resp, dict) and resp.get("queued"):
                    queued += 1

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for _ in range(workers):
            ex.submit(worker)
    sent_sec = time.perf_counter() - t0
    sent = len(lat_ms)
    ok = codes.get("202", 0)

    # 보낸 핑이 Redis Streams → 소비자 그룹 → 저장소 반영까지 다 처리될 때까지 기다림 (최대 30초)
    target = before.get("processed", 0) + before.get("ignored", 0) + queued
    drained_at = None
    for i in range(30):
        st = gps_status(w)
        if st.get("processed", 0) + st.get("ignored", 0) >= target:
            drained_at = i
            break
        time.sleep(1)
    after = gps_status(w)
    stream = redis_stream_state()
    handled = (after.get("processed", 0) - before.get("processed", 0)) + (after.get("ignored", 0) - before.get("ignored", 0))
    code, k, _ = call("GET", "/admin/kpi", w.admin)                     # 관제 반영 지연 p95 (XADD → 저장소 반영)
    relay = next((r for r in (k.get("rows") or []) if r["key"] == "relay_p95_sec"), {}) if isinstance(k, dict) else {}
    return {
        "target_rps": rps, "seconds": seconds, "workers": workers, "runs": len(runs),
        "sent": sent, "achieved_rps": round(sent / sent_sec, 1) if sent_sec else None,
        "accepted_202": ok, "error_pct": round(100 * (sent - ok) / sent, 2) if sent else None, "status_codes": codes,
        "latency": ms_summary(lat_ms), "queued_to_redis": queued,
        "server_handled": handled, "lost": max(0, queued - handled),
        "drain_sec": drained_at, "worker_errors": after.get("errors", 0) - before.get("errors", 0),
        "relay_p95": relay.get("display"), "redis": stream,
    }


# ======================================================================= 1단계: 스모크 테스트
def smoke(w: World, password: str, mon: ResourceMonitor) -> dict:
    """기사 5명 · 주문 1건으로 전체 흐름이 예외 없이 정확히 통과 · 실패 판정을 내리는지 체크리스트."""
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, note: str = "") -> bool:
        checks.append((name, bool(ok), note))
        log(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {note}" if note else ""))
        return bool(ok)

    check("서버 연결 (/health)", call("GET", "/health")[0] == 200)
    mon.phase = "기사 가입"
    # 1-1) 기사 먼저 생성 — 서울/인천 거점 주변, 2.5톤 이상 위주 (서울 → 부산 2.5톤 화물을 받을 수 있게)
    d = seed_drivers(w, 5, hub="서울/인천", trucks=["2.5톤 카고", "5톤 윙바디", "2.5톤 카고", "11톤 탑차", "5톤 윙바디"])
    check("기사 5명 가입 (노란 번호판 · 사업자번호 Mod-10 · 서류 3종 · 중복 검사 통과)", d["registered"] == 5,
          f"{d['registered']}/5" + (f" · {d['fail_reasons'][0]}" if d["fail_reasons"] else ""))
    check("관제 승인 5명 → 매칭 후보", d["approved"] == d["registered"] == 5, f"{d['approved']}/5")
    # 잘못된 가입은 정확히 막혀야 한다 (규칙 엔진 실패 판정)
    code, body, _ = register_driver(w, 900, "서울 중구", "2.5톤 카고", plate_no="서울 12가 3456")
    check("흰색 번호판(자가용) 기사 가입 차단", code == 400, detail(body))
    code, body, _ = register_driver(w, 901, "서울 중구", "2.5톤 카고", biz="1234567890")
    check("사업자번호 검증 숫자 오류 가입 차단", code == 400, detail(body))
    login_seed_drivers(w, password)
    # 1-2) 주문 먼저 생성 — 서울에서 부산으로 2.5톤
    mon.phase = "주문 생성"
    w.round_log = RoundCollector(w).begin()
    mt, mout = start_matching(w, accept_rate=1.0, limit_sec=90)        # 매칭 감시를 주문보다 먼저 시작
    o = seed_orders(w, 1, fixed={"truck_type": "2.5톤 카고", "origin": "서울 금천구 가산동", "destination": "부산 사상구"})
    check("주문 1건 생성 (서울 → 부산 2.5톤 · 사전 검증 통과)", o["created"] == 1,
          o["fail_reasons"][0] if o["fail_reasons"] else f"{o['latency'].get('avg_ms')}ms")
    code, body, _ = call("POST", "/shipper/orders", w.shipper, json={"title": "잘못된 주문", "truck_type": "2.5톤 카고",
                                                                      "origin": "서울 중구", "destination": "서울 중구",
                                                                      "price": 100000})
    check("상차지 = 하차지 주문 차단", code == 400, detail(body))
    # 1-3) 매칭 → 수락 → 운행 → GPS 핑
    mon.phase = "매칭"
    m = finish_matching(mt, mout, 90)
    mon.phase = "GPS 핑"
    check("헝가리안 매칭 → 기사에게 제안 도착", m["offered"] == 1, f"{m['first_offer_sec']['p50']}초")
    check("기사 수락 → 배차 확정 → 운행 시작", m["runs_started"] == 1,
          ", ".join(f"{k} {v}건" for k, v in m["fail_reasons"].items()) or "")
    g = gps_flood(w, rps=5, seconds=4, workers=2) if w.runs else {"skipped": "운행 없음"}
    check("GPS 핑 전송 (202) → Redis Streams 처리", not g.get("skipped") and g["accepted_202"] == g["sent"]
          and g["lost"] == 0, f"{g.get('accepted_202')}/{g.get('sent')} · 유실 {g.get('lost')}" if not g.get("skipped")
          else g["skipped"])
    if w.runs:                                     # 배정 기사가 아닌 단말의 핑은 403 + 재위탁 의심 기록
        r = w.runs[0]
        other = next((v["token"] for k, v in w.drivers.items() if k != r["driver_id"]), None)
        code, body, _ = call("POST", "/tracking/ping", other, json={"order_id": r["order_id"], "lat": r["lat"],
                                                                     "lon": r["lon"], "speed": 70})
        check("배정 기사가 아닌 단말의 핑 차단 (불법 재위탁 의심)", code == 403, detail(body))
    passed = sum(ok for _, ok, _ in checks)
    return {"mode": 1, "checks": [{"name": n, "ok": ok, "note": note} for n, ok, note in checks],
            "passed": passed, "total": len(checks), "drivers": d, "orders": o, "matching": m, "gps": g}


# ======================================================================= 2단계: 효율성 검증
def efficiency(w: World, password: str, mon: ResourceMonitor, a) -> dict:
    """기사 a.drivers 명 · 주문 a.orders 건 → 매칭 소화 시간 · 서버 라운드 지연 → GPS 핑 a.rps 건/초."""
    mon.phase = "기사 가입"
    d = seed_drivers(w, a.drivers)
    login_seed_drivers(w, password)
    mon.phase = "주문 생성"
    w.round_log = RoundCollector(w).begin()
    log(f"매칭 감시 시작 → 주문 {a.orders}건 생성 → 수락(확률 {a.accept_rate:.0%}) → 운행 시작 (최대 {a.match_seconds}초) ...")
    mt, mout = start_matching(w, accept_rate=a.accept_rate, limit_sec=a.match_seconds)
    o = seed_orders(w, a.orders)
    mon.phase = "매칭"
    m = finish_matching(mt, mout, a.match_seconds)
    log(f"  첫 제안 p95 {m['first_offer_sec']['p95']}초 · 5초 안에 제안 {m['first_offer_within_5s_pct']}% · "
        f"배차 확정 {m['assigned']}/{m['orders']} · 운행 {m['runs_started']}건")
    mon.phase = "GPS 핑"
    log(f"GPS 핑 초당 {a.rps}건 × {a.gps_seconds}초 (스레드 {a.workers}개 · 운행 {len(w.runs)}건) ...")
    g = gps_flood(w, rps=a.rps, seconds=a.gps_seconds, workers=a.workers)
    mon.phase = "정리"
    return {"mode": 2, "drivers": d, "orders": o, "matching": m, "gps": g}


# ======================================================================= 결과 정리
def server_perf(w: World, mon: ResourceMonitor, since: float) -> dict:
    """서버 성능 기록(GET /admin/perf)을 단계별로 요약: DB 저장 · 이벤트 루프 멈춤 · 느린 요청 · 긴 GC."""
    code, body, _ = call("GET", "/admin/perf", w.admin, params={"since": since}, timeout=20)
    if code != 200 or not isinstance(body, dict):
        return {"error": f"/admin/perf {code} — 서버 이미지가 측정 기능 이전 버전일 수 있음 (docker compose up --build -d)"}
    out: dict[str, dict] = {}
    for sv in body.get("save_log") or []:
        ph = out.setdefault(mon.phase_at(sv["t"]), {"saves": [], "stall": [], "request": [], "gc": []})
        ph["saves"].append(sv)
    for e in body.get("events") or []:
        ph = out.setdefault(mon.phase_at(e["t"]), {"saves": [], "stall": [], "request": [], "gc": []})
        ph.setdefault(e["kind"], []).append(e)
    summary = {}
    for name, ph in out.items():
        sv = ph["saves"]
        worst = max(sv, key=lambda x: x["ms"], default=None)
        reqs = sorted(ph["request"], key=lambda x: -x["ms"])
        summary[name] = {
            "saves": len(sv), "save_avg_ms": round(statistics.mean(x["ms"] for x in sv), 1) if sv else None,
            "save_max": worst,
            "slow_saves": sum(1 for x in sv if x["ms"] >= 200),
            "stalls": len(ph["stall"]), "stall_max_ms": max((x["ms"] for x in ph["stall"]), default=None),
            "stall_total_ms": round(sum(x["ms"] for x in ph["stall"]), 1),
            "slow_requests": len(reqs), "slow_request_top": reqs[:3],
            "gc": len(ph["gc"]), "gc_max": max(ph["gc"], key=lambda x: x["ms"], default=None),
        }
    return {"overall": body.get("saves"), "by_phase": summary}


def perf_lines(pf: dict) -> list[str]:
    """서버 성능 기록 → 보고서 줄."""
    if not pf:
        return []
    if pf.get("error"):
        return [f"- 서버 성능 기록: {pf['error']}"]
    o = pf.get("overall") or {}
    L = [f"- 서버 DB 저장: {o.get('count')}회 · 평균 {o.get('avg_ms')} · p95 {o.get('p95_ms')} · 최대 {o.get('max_ms')}ms"]
    for name, p in (pf.get("by_phase") or {}).items():
        line = f"  [{name}] 저장 {p['saves']}회"
        if p["save_max"]:
            m = p["save_max"]
            ch = " · ".join(f"{k} {v}" for k, v in (m.get("changed") or {}).items())
            line += (f" (평균 {p['save_avg_ms']}ms · 최대 {m['ms']}ms = 행 만들기 {m['rows_ms']} + 비교 {m['diff_ms']} + "
                     f"DB 쓰기 {m['db_ms']} · 바뀐 행: {ch or '-'})")
        if p["stalls"]:
            line += f" · 루프 멈춤 {p['stalls']}회 (최대 {p['stall_max_ms']}ms · 합 {p['stall_total_ms']}ms)"
        if p["slow_requests"]:
            top = p["slow_request_top"][0]
            line += (f" · 1초 넘는 요청 {p['slow_requests']}건 (최대 {top['method']} {top['path']} {top['ms']}ms = "
                     f"처리 {top.get('handler_ms')} + 저장 {top.get('persist_ms')})")
        if p["gc"]:
            g = p["gc_max"]
            line += f" · 긴 GC {p['gc']}회 (최대 {g['ms']}ms · 세대 {g.get('generation')})"
        L.append(line)
    return L


def verdicts(res: dict) -> list[str]:
    """측정값 → 판정 문장 (효율성 기준: 매칭 3초 · 5초 주기 · 유실 0 · 오류 1% 미만)."""
    out = []
    m, g = res.get("matching") or {}, res.get("gps") or {}
    p95 = (m.get("server_round_ms") or {}).get("p95_ms")
    if p95 is not None:
        out.append(f"{'✅' if p95 < 3000 else '⚠️'} 서버 매칭 라운드 p95 {p95 / 1000:.2f}초 (기준 < 3초 · 넘으면 쿼리 인덱스 · 알고리즘 루프 튜닝)")
    if m.get("first_offer_within_5s_pct") is not None:
        v = m["first_offer_within_5s_pct"]
        out.append(f"{'✅' if v >= 100 else '⚠️'} 주문 {m['orders']}건 중 {v}% 가 5초 안에 첫 제안 (기준 100%)")
    if g and not g.get("skipped"):
        out.append(f"{'✅' if g['lost'] == 0 else '❌'} GPS 핑 유실 {g['lost']}건 (Redis 로 보낸 {g['queued_to_redis']:,}건 중)")
        out.append(f"{'✅' if (g['error_pct'] or 0) < 1 else '⚠️'} GPS 핑 실패율 {g['error_pct']}% · "
                   f"실제 처리량 초당 {g['achieved_rps']}건 (목표 {g['target_rps']}) · p95 {g['latency'].get('p95_ms')}ms")
    return out


def report_text(res: dict) -> str:
    """사람이 읽는 결과 요약 (터미널 출력 · 파일 맨 아래 블록 공용)."""
    L = [f"## {'1단계 스모크 테스트' if res['mode'] == 1 else '2단계 효율성 검증'} — {res['started']} · {res['elapsed_sec']}초"]
    if res.get("error"):
        L.append(f"⚠️ 중단: {res['error']}")
    if res["mode"] == 1 and res.get("checks"):
        L.append(f"체크리스트 {res['passed']}/{res['total']} 통과")
        L += [f"  [{'PASS' if c['ok'] else 'FAIL'}] {c['name']}" + (f" — {c['note']}" if c["note"] else "")
              for c in res["checks"]]
    d, o, m, g = (res.get(k) or {} for k in ("drivers", "orders", "matching", "gps"))
    if d:
        L.append(f"- 기사 가입 {d['registered']:,}/{d['requested']:,}명 · {d['register_sec']}초 (초당 {d['register_per_sec']}명) · "
                 f"가입 지연 p95 {d['register_latency'].get('p95_ms')}ms · 승인 {d['approved']:,}명 {d['approve_sec']}초")
    if o:
        L.append(f"- 주문 생성 {o['created']:,}/{o['requested']:,}건 · {o['sec']}초 · 지연 p50 {o['latency'].get('p50_ms')} · "
                 f"p95 {o['latency'].get('p95_ms')} · 최대 {o['latency'].get('max_ms')}ms")
    if m:
        sr = m.get("server_round_ms") or {}
        L.append(f"- 매칭: 첫 제안 p50 {m['first_offer_sec']['p50']} · p95 {m['first_offer_sec']['p95']} · 최대 "
                 f"{m['first_offer_sec']['max']}초 · 5초 안에 {m['first_offer_within_5s_pct']}% · 배차 확정 "
                 f"{m['assigned']}/{m['orders']}건 (p95 {m['assigned_sec']['p95']}초 · 거절 후 재배정 {m['declines']}건)")
        br = m.get("biggest_round") or {}
        if sr.get("count"):
            L.append(f"- 서버 매칭 라운드 (PostGIS ST_DWithin → 점수 → LightGBM → 헝가리안): {sr['count']}회 · "
                     f"평균 {sr.get('avg_ms')} · p95 {sr.get('p95_ms')} · 최대 {sr.get('max_ms')}ms · 가장 큰 라운드 "
                     f"주문 {br.get('orders')} × 기사 {br.get('drivers')} (후보 쌍 {br.get('candidate_pairs')}) "
                     f"{br.get('latency_ms')}ms")
        else:
            L.append("- 서버 매칭 라운드: 수집된 라운드 없음 (이전 버전 결과)")
        if m.get("fail_reasons"):
            L.append("- 배차 안 된 주문 사유: " + " · ".join(f"{k} {v}건" for k, v in m["fail_reasons"].items()))
    if g and not g.get("skipped"):
        rd = g.get("redis") or {}
        L.append(f"- GPS 핑: 목표 초당 {g['target_rps']} × {g['seconds']}초 → 보냄 {g['sent']:,}건 (실제 초당 {g['achieved_rps']}) · "
                 f"202 {g['accepted_202']:,} · 실패 {g['error_pct']}% · 지연 p50 {g['latency'].get('p50_ms')} · "
                 f"p95 {g['latency'].get('p95_ms')} · p99 {g['latency'].get('p99_ms')}ms")
        L.append(f"- Redis Streams: 보낸 {g['queued_to_redis']:,} → 처리 {g['server_handled']:,} · 유실 {g['lost']} · "
                 f"다 처리까지 {g['drain_sec']}초 · 관제 반영 지연 p95 {g['relay_p95']} · 스트림 길이 {rd.get('xlen')} · "
                 f"미처리 {rd.get('pending')} · 안 읽음 {rd.get('lag')}")
    elif g.get("skipped"):
        L.append(f"- GPS 핑: {g['skipped']}")
    for role, r in (res.get("resources") or {}).items():
        phases = " · ".join(f"{p} {c}%" for p, c in r["cpu_max_by_phase"].items())
        L.append(f"- 자원 {role}: CPU 최대 {r['cpu_max']}% (평균 {r['cpu_avg']}%) [{phases}] · 메모리 "
                 f"{r['mem_start_mib']} → 최대 {r['mem_max_mib']} → 끝 {r['mem_end_mib']} MiB")
    L += perf_lines(res.get("perf") or {})
    v = verdicts(res)
    if v:
        L += ["", "판정"] + [f"  {x}" for x in v]
    return "\n".join(L)


# 결과 블록 표시줄 — 문자열을 나눠 이어 붙여서, 이 코드 자체에는 표시줄 전체가 나오지 않게 한다
# (전체가 코드에 그대로 있으면 결과를 저장할 때 코드가 잘려 나간다)
RESULT_MARK = "# ===== " + "마지막 실행 결과 (실행이 끝나면 자동으로 바뀜 · 문자열이라 코드로 실행되지 않음) ====="


def save_results(res: dict) -> None:
    """결과 JSON 저장 + 이 파일 맨 아래 ''' ''' 블록을 모드별 최신 결과로 다시 쓴다."""
    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    for name in (f"mode{res['mode']}_{stamp}.json", f"mode{res['mode']}_latest.json"):
        (RESULTS / name).write_text(json.dumps(res, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    texts = []
    for mode in (1, 2):
        f = RESULTS / f"mode{mode}_latest.json"
        if f.exists():
            try:
                texts.append(report_text(json.loads(f.read_text(encoding="utf-8"))))
            except (ValueError, KeyError):
                pass
    me = Path(__file__).resolve()
    src = me.read_text(encoding="utf-8")
    code = src[:src.rfind(RESULT_MARK)].rstrip() if RESULT_MARK in src else src.rstrip()   # 마지막 표시줄 앞까지 = 코드
    if "def main(" not in code:                          # 안전장치: 코드가 잘릴 상황이면 파일을 건드리지 않는다
        log("결과 블록 저장을 건너뜀 (파일 구조가 예상과 다름) — 결과는 scripts/results 에 저장됨")
        return
    body = "\n\n".join(texts).replace("\\", "/").replace("'''", "’’’")
    me.write_text(f"{code}\n\n\n{RESULT_MARK}\n'''\n{body}\n'''\n", encoding="utf-8")
    log(f"결과 저장: scripts/results/mode{res['mode']}_latest.json · 이 파일 맨 아래 블록")


# ======================================================================= 실행부
def main() -> int:
    ap = argparse.ArgumentParser(description="배차의 민족 통합 시뮬레이터 (1 스모크 · 2 효율성)")
    ap.add_argument("--mode", choices=["1", "2"], help="1 스모크 · 2 효율성 (없으면 메뉴)")
    ap.add_argument("--drivers", type=int, default=1000, help="2단계 기사 수 (기본 1,000 · 500~1,000 권장)")
    ap.add_argument("--orders", type=int, default=100, help="2단계 주문 수 (기본 100 · 한꺼번에)")
    ap.add_argument("--accept-rate", type=float, default=0.9, help="기사 수락 확률 (기본 0.9 · 나머지는 거절 → 재배정)")
    ap.add_argument("--match-seconds", type=int, default=180, help="매칭 · 수락을 기다리는 최대 시간 (기본 180초)")
    ap.add_argument("--rps", type=int, default=200, help="GPS 핑 목표 초당 건수 (기본 200)")
    ap.add_argument("--gps-seconds", type=int, default=30, help="GPS 핑을 보내는 시간 (기본 30초)")
    ap.add_argument("--workers", type=int, default=64, help="GPS 핑 동시 스레드 수 (기본 64)")
    ap.add_argument("--seed", type=int, default=7, help="무작위 시드 (같으면 같은 기사 · 주문 구성)")
    ap.add_argument("--keep", action="store_true", help="끝나도 테스트 스택을 지우지 않음")
    a = ap.parse_args()

    mode = a.mode
    if not mode:                                    # 메뉴 (VS Code ▷ 로 실행하면 여기로)
        print("\n배차의 민족 통합 시뮬레이터 — 시연 서버와 분리된 테스트 전용 스택에서 실행합니다")
        print("  1. 스모크 테스트 실행 (기사 5명 · 주문 1건 · 약 1~2분)")
        print(f"  2. 효율성 검증 실행 (기사 {a.drivers:,}명 · 주문 {a.orders}건 · GPS 핑 초당 {a.rps}건 · 약 5~10분)")
        mode = input("원하는 테스트 번호를 입력하세요: ").strip()
        if mode not in ("1", "2"):
            print("1 또는 2 를 입력하세요.")
            return 2
    mode = int(mode)

    if not check_docker():
        return 1
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    t0 = time.time()
    res: dict = {"mode": mode, "started": started, "options": vars(a)}
    mon = ResourceMonitor()
    try:
        if not stack_up():
            return 1
        password = demo_password()
        w = World(a.seed)
        w.load_meta()
        # 관제 · 화주 로그인 (시드 데이터 계정 · 테스트 스택 안에서만)
        for role, email in (("admin", ADMIN_EMAIL), ("shipper", SHIPPER_EMAIL)):
            code, body, _ = call("POST", "/auth/login", json={"email": email, "password": password})
            if code != 200:
                raise RuntimeError(f"{role} 로그인 실패: {detail(body)}")
            setattr(w, role, body["token"])
        mon.start()
        test_start = time.time()
        res.update(smoke(w, password, mon) if mode == 1 else efficiency(w, password, mon, a))
        res["perf"] = server_perf(w, mon, since=test_start - 1)     # 테스트 스택을 지우기 전에 받아 둔다
    except KeyboardInterrupt:
        res["error"] = "사용자가 중단 (Ctrl+C)"
    except Exception as e:  # noqa: BLE001 — 어떤 오류든 지금까지 결과는 남긴다
        res["error"] = f"{type(e).__name__}: {e}"
        log(f"오류로 중단: {res['error']}")
    finally:
        mon.stop()
        res["resources"] = mon.summary()
        res["elapsed_sec"] = round(time.time() - t0, 1)
        try:
            stack_down(a.keep)
        except subprocess.TimeoutExpired:
            log("테스트 스택 정리 시간 초과 — 직접: docker compose -f scripts/docker-compose.loadtest.yml down -v")
    text = report_text(res)
    print("\n" + text + "\n")
    save_results(res)
    return 0 if not res.get("error") and (mode != 1 or res.get("passed") == res.get("total")) else 1


if __name__ == "__main__":
    sys.exit(main())


# ===== 마지막 실행 결과 (실행이 끝나면 자동으로 바뀜 · 문자열이라 코드로 실행되지 않음) =====
'''
## 1단계 스모크 테스트 — 2026-11-18 11:07:22 · 28.3초
체크리스트 11/11 통과
  [PASS] 서버 연결 (/health)
  [PASS] 기사 5명 가입 (노란 번호판 · 사업자번호 Mod-10 · 서류 3종 · 중복 검사 통과) — 5/5
  [PASS] 관제 승인 5명 → 매칭 후보 — 5/5
  [PASS] 흰색 번호판(자가용) 기사 가입 차단 — 자가용 (흰색 번호판) 차량은 등록할 수 없습니다. 화물 운송은 사업용 노란색 번호판(바 · 사 · 아 · 자)만 가능합니다.
  [PASS] 사업자번호 검증 숫자 오류 가입 차단 — 사업자등록번호 확인 실패: 검증 숫자 오류 (Mod-10) (검증 숫자는 1 이어야 합니다) · 모의 판정 (국세청 연동 전)
  [PASS] 주문 1건 생성 (서울 → 부산 2.5톤 · 사전 검증 통과) — 2152.1ms
  [PASS] 상차지 = 하차지 주문 차단 — 상차지와 하차지가 같은 지역입니다.
  [PASS] 헝가리안 매칭 → 기사에게 제안 도착 — 0.05초
  [PASS] 기사 수락 → 배차 확정 → 운행 시작
  [PASS] GPS 핑 전송 (202) → Redis Streams 처리 — 20/20 · 유실 0
  [PASS] 배정 기사가 아닌 단말의 핑 차단 (불법 재위탁 의심) — 배정된 기사의 단말이 아닙니다. 불법 재위탁 의심으로 관제에 보고되었습니다.
- 기사 가입 5/5명 · 2.3초 (초당 2.1명) · 가입 지연 p95 2327.3ms · 승인 5명 2.2초
- 주문 생성 1/1건 · 2.15초 · 지연 p50 2152.1 · p95 2152.1 · 최대 2152.1ms
- 매칭: 첫 제안 p50 0.05 · p95 0.05 · 최대 0.05초 · 5초 안에 100.0% · 배차 확정 1/1건 (p95 2.22초 · 거절 후 재배정 0건)
- 서버 매칭 라운드 (PostGIS ST_DWithin → 점수 → LightGBM → 헝가리안): 1회 · 평균 20.1 · p95 20.1 · 최대 20.1ms · 가장 큰 라운드 주문 5 × 기사 5 (후보 쌍 5) 20.07ms
- GPS 핑: 목표 초당 5 × 4초 → 보냄 20건 (실제 초당 4.6) · 202 20 · 실패 0.0% · 지연 p50 5.2 · p95 2052.6 · p99 2130.9ms
- Redis Streams: 보낸 20 → 처리 20 · 유실 0 · 다 처리까지 0초 · 관제 반영 지연 p95 0.00초 · 스트림 길이 20 · 미처리 0 · 안 읽음 0
- 자원 backend: CPU 최대 248.9% (평균 64.8%) [기사 가입 3.0% · 주문 생성 0.3% · GPS 핑 248.9%] · 메모리 174.9 → 최대 176.2 → 끝 175.9 MiB
- 자원 db: CPU 최대 5.3% (평균 2.8%) [기사 가입 0.0% · 주문 생성 3.5% · GPS 핑 5.3%] · 메모리 43.8 → 최대 46.8 → 끝 46.8 MiB
- 자원 redis: CPU 최대 3.5% (평균 1.3%) [기사 가입 0.3% · 주문 생성 0.6% · GPS 핑 3.5%] · 메모리 5.4 → 최대 5.4 → 끝 5.1 MiB

판정
  ✅ 서버 매칭 라운드 p95 0.02초 (기준 < 3초 · 넘으면 쿼리 인덱스 · 알고리즘 루프 튜닝)
  ✅ 주문 1건 중 100.0% 가 5초 안에 첫 제안 (기준 100%)
  ✅ GPS 핑 유실 0건 (Redis 로 보낸 20건 중)
  ✅ GPS 핑 실패율 0.0% · 실제 처리량 초당 4.6건 (목표 5) · p95 2052.6ms

## 2단계 효율성 검증 — 2026-11-18 11:40:19 · 151.7초
- 기사 가입 1,000/1,000명 · 61.7초 (초당 16.2명) · 가입 지연 p95 635.7ms · 승인 1,000명 27.9초
- 주문 생성 100/100건 · 7.41초 · 지연 p50 1176.5 · p95 3075.2 · 최대 3631.2ms
- 매칭: 첫 제안 p50 0.47 · p95 1.36 · 최대 1.36초 · 5초 안에 100.0% · 배차 확정 100/100건 (p95 4.19초 · 거절 후 재배정 10건)
- 서버 매칭 라운드 (PostGIS ST_DWithin → 점수 → LightGBM → 헝가리안): 77회 · 평균 21.7 · p95 30.9 · 최대 33.8ms · 가장 큰 라운드 주문 1 × 기사 113 (후보 쌍 113) 21.66ms
- GPS 핑: 목표 초당 200 × 30초 → 보냄 6,000건 (실제 초당 196.7) · 202 6,000 · 실패 0.0% · 지연 p50 3.8 · p95 338.2 · p99 2264.1ms
- Redis Streams: 보낸 6,000 → 처리 6,000 · 유실 0 · 다 처리까지 0초 · 관제 반영 지연 p95 0.05초 · 스트림 길이 6000 · 미처리 0 · 안 읽음 0
- 자원 backend: CPU 최대 649.3% (평균 111.0%) [기사 가입 649.3% · 주문 생성 439.0% · 매칭 379.6% · GPS 핑 79.5%] · 메모리 187.3 → 최대 212.1 → 끝 210.7 MiB
- 자원 db: CPU 최대 20.3% (평균 4.4%) [기사 가입 20.3% · 주문 생성 6.4% · 매칭 6.6% · GPS 핑 5.8%] · 메모리 61.3 → 최대 107.3 → 끝 102.4 MiB
- 자원 redis: CPU 최대 7.7% (평균 2.4%) [기사 가입 5.1% · 주문 생성 2.9% · 매칭 1.4% · GPS 핑 7.7%] · 메모리 5.8 → 최대 10.2 → 끝 7.0 MiB
- 서버 DB 저장: 242회 · 평균 122.4 · p95 231.4 · 최대 296.2ms
  [준비] 저장 1회 (평균 39.1ms · 최대 39.1ms = 행 만들기 10.1 + 비교 7.5 + DB 쓰기 21.5 · 바뀐 행: auth_tokens 1 · orders 11 · matching_metrics_logs 1) · 루프 멈춤 1회 (최대 365.1ms · 합 365.1ms)
  [기사 가입] 저장 199회 (평균 108.4ms · 최대 251.2ms = 행 만들기 143.7 + 비교 80.0 + DB 쓰기 27.5 · 바뀐 행: drivers 26 · orders 9 · audit_logs 26) · 루프 멈춤 7회 (최대 395.2ms · 합 2306.5ms) · 긴 GC 29회 (최대 106.7ms · 세대 2)
  [주문 생성] 저장 17회 (평균 173.4ms · 최대 252.9ms = 행 만들기 147.5 + 비교 83.0 + DB 쓰기 22.4 · 바뀐 행: orders 13 · audit_logs 2 · matching_metrics_logs 1 · app_state 1) · 루프 멈춤 4회 (최대 389.2ms · 합 1374.4ms) · 1초 넘는 요청 11건 (최대 POST /api/v1/shipper/orders 1223.7ms = 처리 1223.7 + 저장 0.0) · 긴 GC 2회 (최대 96.6ms · 세대 2)
  [매칭] 저장 6회 (평균 241.3ms · 최대 296.2ms = 행 만들기 147.0 + 비교 87.6 + DB 쓰기 61.6 · 바뀐 행: orders 67 · audit_logs 1) · 긴 GC 1회 (최대 91.6ms · 세대 2)
  [GPS 핑] 저장 19회 (평균 190.0ms · 최대 241.7ms = 행 만들기 153.8 + 비교 87.7 + DB 쓰기 0.2 · 바뀐 행: -) · 루프 멈춤 1회 (최대 325.2ms · 합 325.2ms) · 긴 GC 4회 (최대 97.5ms · 세대 2)

판정
  ✅ 서버 매칭 라운드 p95 0.03초 (기준 < 3초 · 넘으면 쿼리 인덱스 · 알고리즘 루프 튜닝)
  ✅ 주문 100건 중 100.0% 가 5초 안에 첫 제안 (기준 100%)
  ✅ GPS 핑 유실 0건 (Redis 로 보낸 6,000건 중)
  ✅ GPS 핑 실패율 0.0% · 실제 처리량 초당 196.7건 (목표 200) · p95 338.2ms
'''
