# 배차의 민족 — B2B 물류 배차 SaaS (b2b-logistics-saas)

화주가 화물을 등록하면 **3D 적재 시뮬레이션**으로 전량 실리는 차량만 골라,
**PostGIS 반경 필터 → 규칙 점수 + LightGBM 수락 예측 → 헝가리안 최적 배정**으로 기사에게 배차를 제안하는 플랫폼입니다.
화주 · 차주 · 통합관제 3가지 역할 화면과, 차주용 휴대폰 3D 적재 가이드를 제공합니다.

> **시연용 모의 · 가상 데이터 안내**
> - **GPS**: 실제 차량 GPS가 아니라 `simulator/virtual_driver.py` 가 도로 경로를 따라 만든 **모의 데이터**입니다.
> - **LightGBM 수락 예측**: 실운행 기록이 없어 **가상 데이터(기사 수락 행동 시뮬레이션 8,000건)** 로 학습했습니다. 화면에도 표시됩니다.
> - **결제(PG)**: `MockPG` **모의 결제**입니다. 카드 · 계좌 정보를 받지 않고 실제 출금도 없습니다.
> - **AI 관제 보조**: 외부 LLM · API 키 없이 동작하는 **규칙 기반** 에이전트입니다.

---

## 빠른 실행 (도커)

```bash
copy .env.example .env          # 처음 한 번 · change-me 를 원하는 비밀번호로
docker compose up --build -d    # db · redis · backend · worker · simulator · frontend
```

| 주소 | 내용 |
|---|---|
| http://localhost:8501 | 웹 화면 (화주 · 차주 · 관제) |
| http://localhost:8000/docs | API 문서 |
| http://localhost:8000/m/ | 차주용 휴대폰 3D 적재 가이드 (휴대폰: `http://<PC IP>:8000/m/`) |

데모 계정 (비밀번호 `baecha1234`): 화주 `logistics@samsung.com` · 차주 `driver.kim@baecha.co.kr` · 관제 `admin.control@baecha.co.kr`
VS Code + Docker 로 실행하는 단계별 방법과 문제 해결은 **실행방법.txt** 를 보세요.

---

## 아키텍처

```
 [Streamlit 웹]   [모바일 웹 (Three.js)]                [가상 기사 시뮬레이터] ──XADD──▶ Redis Stream gps:stream
       │                 │                                                                   │
       └──── REST /api/v1 ────▶  FastAPI (backend) ◀── XREADGROUP (gps_stream_worker) ────────┘
                                   │   │   │
            PostgreSQL + PostGIS ◀─┘   │   └─▶ Redis 분산 락 (매칭 라운드)
           (화물 · 기사 위치 · 감사 로그)   │
                                       └─▶ Celery (Redis 브로커) ──▶ worker: 3D 빈패킹 비동기 계산
```

### 3단계 하이브리드 매칭
1. **공간 필터** `engines/matcher/spatial.py` — PostGIS `ST_DWithin` 으로 상차지 반경 안의 기사만
2. **스코어링** — 기사 차량 기준 3D 적재 시뮬레이션(전량 적재 · 필수 제약) 통과 차량에 대해
   규칙 점수(거리 60% + 적재율 40%, 관제가 변경 가능) **70%** + `ml_ranker.py` LightGBM 수락 예측 **30%**
3. **최적 배정** `hungarian.py` — 대기 화물 전체 × 빈 기사 전체를 헝가리안 알고리즘으로 1:1 배정
   → 배정된 기사에게 제안, 거절 · 시간 초과면 다시 배정 (Redis 분산 락 안에서 실행)

### 주문 상태 FSM · 감사 로그
`engines/fsm/order_fsm.py` 의 전이표로만 상태가 바뀝니다. 모든 전이(특히 청구 · 결제 · 취소)는 감사 로그에 남습니다.
```
화물  MATCHING → ASSIGNED → IN_TRANSIT → DELIVERED ⇄ SETTLED
청구  UNBILLED → REQUESTED → PAID → REFUNDED → (재청구) REQUESTED
```

---

## 폴더 구조

```
수정본/  (b2b-logistics-saas)
├── .github/workflows/ci.yml   # CI: pyflakes · 점검 스크립트 (3.12/3.14) + docker compose 통합 점검
├── .env.example / .env        # 환경변수 템플릿 / 로컬 실제 값 (.env 는 git 제외)
├── docker-compose.yml         # db · redis · backend · worker · simulator · frontend
├── Dockerfile                 # 백엔드/워커/화면 공용 이미지 (Python 3.14)
├── requirements.txt           # FastAPI · Streamlit · SQLAlchemy · PostGIS · Redis · Celery · LightGBM · py3dbp …
├── backend/
│   ├── main.py                # 앱 진입점 (CORS · 요청 후 DB 저장 · GPS 워커 시작 · /m 모바일 웹)
│   ├── config.py              # pydantic-settings (.env)
│   ├── core/                  # database.py(연결 · 저장소 · DB 동기화) · security.py · redis_client.py · seed.py · errors.py
│   ├── models/                # user.py(users · shippers · drivers · tokens) · order.py(orders · anomalies · audit_logs)
│   ├── schemas/               # auth_schema · order_schema · settlement_schema
│   ├── services/              # auth · order · matching · billing(AI 운임 · MockPG · 인보이스)
│   ├── engines/
│   │   ├── packer/            # 3D 빈패킹 (EP 휴리스틱 · py3dbp_engine.py · 기하 검증)
│   │   ├── matcher/           # spatial.py(PostGIS) · ml_ranker.py(LightGBM) · hungarian.py
│   │   ├── fsm/order_fsm.py   # 상태 전이 · 감사 로그
│   │   └── ai/rule_agent.py   # 규칙 기반 AI 관제 보조
│   ├── workers/               # celery_app.py · packing_tasks.py · gps_stream_worker.py
│   ├── api/                   # v1.py · endpoints/(auth · orders · matching · tracking · settlement)
│   └── data/                  # route_cache.json(도로 경로 990개) · ml_ranker.txt/.json(학습된 모델)
├── frontend/
│   ├── app.py                 # 로그인 · 역할별 메뉴
│   ├── pages/                 # 0_auth · 1_shipper_portal · 2_control_tower · 3_admin_compliance · 4_driver_app
│   └── static/style.css       # 전역 스타일
├── mobile_web/                # index.html · js/load_visualizer.js · css/mobile_style.css (Three.js r147 CDN)
├── simulator/                 # virtual_driver.py(GPS) · virtual_shipper.py(가상 주문 · 부하 테스트)
└── tests/                     # conftest.py · test_suites.py(pytest) · test_packing · test_matching · test_fsm · smoke_test
```

## 화면

| 역할 | 메뉴 |
|---|---|
| 화주 | 배차 · 실시간 관제 / 신규 화물 등록(운송 정보 → 적재 품목 → 3D 시뮬레이션 → AI 운임) / 정산 · 운송 이력(모의 PG 결제 · 월별 인보이스) |
| 차주 | 배차 제안 · AI 추천 리스트(AI 수락 예측 표시) · 내 운행 / 수익 · 화주 청구 / 휴대폰 3D 적재 가이드 |
| 관제 | 전국 관제 지도(AI 관제 브리핑 · GPS 수신) / 매칭 분석(3단계 엔진 · 설정) / 이상 감지 / 컴플라이언스(가입 승인 · 감사 로그 · 결제 취소) / 데모 데이터 초기화 |

## 테스트

```bash
docker compose exec backend python -m pytest      # 컨테이너 안 (pytest 설치 필요 시: pip install pytest)
python -m pytest                                  # 로컬 (requirements-dev.txt 설치 후)
```
점검 스크립트는 실제 DB 를 쓰지 않습니다(메모리 저장소). `smoke_test.py` 는 실행 중인 서버에 API 로 한 바퀴 돕니다.

## 저장 방식
메모리 저장소(Store)를 작업 사본으로 쓰고, 요청이 끝날 때마다 **바뀐 행만 PostgreSQL 에 UPSERT** 합니다.
서버를 재시작하면 DB 에서 다시 불러옵니다. DB 에 연결할 수 없으면 메모리만으로 동작합니다(재시작 시 초기화).
