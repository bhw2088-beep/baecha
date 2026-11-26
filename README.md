# 배차의 민족 — B2B 물류 배차 SaaS (b2b-logistics-saas)

화주가 화물을 등록하면 **3D 적재 시뮬레이션**으로 전량 실리는 차량만 골라,
**PostGIS 반경 필터 → 규칙 점수 + LightGBM 수락 예측 → 헝가리안 최적 배정**으로 기사에게 배차를 제안하는 플랫폼입니다.
화주 · 차주 · 통합관제 3가지 역할 화면과, 차주용 휴대폰 3D 적재 가이드를 제공합니다.

> **시연용 모의 · 가상 데이터 안내**
> - **GPS**: 실제 차량 GPS가 아니라 `simulator/virtual_driver.py` 가 도로 경로를 따라 만든 **모의 데이터**입니다 (가우스 지터 · 고의 이탈 시나리오 포함).
> - **LightGBM 수락 예측**: 실운행 기록이 없어 **가상 데이터(기사 수락 행동 시뮬레이션 8,000건)** 로 학습했습니다. 화면에도 표시됩니다.
> - **결제(PG)**: `MockPG` **모의 결제**입니다. 카드 · 계좌 정보를 받지 않고 실제 출금도 없습니다.
> - **사업자 확인**: Mod-10 검증 + 모의 휴업/폐업 목록. `.env` 에 `NTS_API_KEY` 를 넣으면 국세청 API 로 조회합니다.
> - **날씨 · 수급**: 확률 분포 모의 기상 · 포아송 가상 콜 기반 D/S 지수입니다.
> - **AI 관제**: `.env` 에 `GEMINI_API_KEY` 가 있으면 Gemini(Function Calling), 없으면 **규칙 기반** 에이전트로 답합니다.

---

## 빠른 실행 (도커)

```bash
copy .env.example .env          # 처음 한 번 · change-me 를 원하는 비밀번호로
docker compose up --build -d    # db · redis · backend · worker · simulator · frontend
```

| 주소 | 내용 |
|---|---|
| http://localhost:8501 | 웹 화면 (화주 · 차주) |
| http://localhost:8502 | **관제센터 전용** 로그인 (통합관제 계정만 · `?mode=control` 불필요) |
| http://localhost:8503 | 차주용 휴대폰 3D 적재 가이드 (휴대폰: `http://<PC IP>:8503`) · 예전 주소 `:8000/m/` 도 그대로 |
| http://localhost:8000/docs | API 문서 |

데모 계정 (비밀번호 `baecha1234`): 화주 `logistics@hanbit.example.com` · 차주 `driver.kim@baecha.co.kr` · 관제 `admin.control@baecha.co.kr`
관제 계정은 **관제센터 전용 주소** http://localhost:8502 (또는 http://localhost:8501/?mode=control) 에서만 접속합니다 (일반 로그인 화면 · 회원가입에는 관제 메뉴 없음).
VS Code + Docker 로 실행하는 단계별 방법과 문제 해결은 **[docs/가이드/실행방법.md](docs/가이드/실행방법.md)** 를 보세요.
적재 · 매칭 · 관제 핵심 코드와 측정값은 **[docs/설명/주요코드_설명.md](docs/설명/주요코드_설명.md)** 에 정리했습니다.

---

## 문서

| 분류 | 문서 | 내용 |
|---|---|---|
| 가이드 | [실행방법.md](docs/가이드/실행방법.md) | VS Code + Docker 단계별 실행 · 문제 해결 |
| 가이드 | [브랜치_전략.md](docs/가이드/브랜치_전략.md) | 브랜치 · PR · CI · 배포 순서 |
| 명세 | [기능명세서.md](docs/명세/기능명세서.md) | 규칙 · 화면 · API 기능 명세 |
| 명세 | [컬럼명_리스트.md](docs/명세/컬럼명_리스트.md) | DB 테이블 · API 응답 필드 |
| 설명 | [주요코드_설명.md](docs/설명/주요코드_설명.md) | 적재 · 매칭 · 관제 핵심 코드 |
| 설명 | [벤치마킹_보고서.md](docs/설명/벤치마킹_보고서.md) | 아키텍처 · KPI 측정값 · 한계 |
| 테스트 결과 | [tests/stress/stress_8k/README.md](tests/stress/stress_8k/README.md) | 8,008건 스트레스 테스트 실행 · 결과 |

---

## 아키텍처

```
 [Streamlit 웹]   [모바일 웹 (Three.js)]        [가상 기사 시뮬레이터] ──POST /tracking/ping──┐
       │                 │                                                                    ▼
       └──── REST /api/v1 ────▶  FastAPI (배정 기사 대조) ──XADD──▶ Redis Stream gps:stream ──┐
                                   ▲                                                         │
                                   └──── XREADGROUP (gps_stream_worker → 이탈 · 지연 판정) ◀─┘
                                   │   │   │
            PostgreSQL + PostGIS ◀─┘   │   └─▶ Redis 분산 락 (매칭 라운드)
           (화물 · 기사 위치 · 감사 로그)   │
                                       └─▶ Celery (Redis 브로커) ──▶ worker: 3D 빈패킹 비동기 계산
```

### 3단계 하이브리드 매칭
1. **하드 필터** — PostGIS `ST_DWithin` 반경 + 관제 승인 + **사업용 노란 번호판** + **기사 톤수 ≥ 필수 최소 톤수** + 블랙리스트 제외
2. **스코어링** — 기사 차량 기준 3D 적재 시뮬레이션(전량 적재 · 안전율 · 축중량 등 필수 제약) 통과 차량에 대해
   규칙 점수(거리 60% + 적재율 40%, 관제가 변경 가능) **70%** + `ml_ranker.py` LightGBM 수락 예측 **30%**
   (피처: 거리 · 적재율 · 운임 · cf_score(권역 선호) · 기사 평점 · 러시아워 등 10개) → **lgb_score ≥ 0.75** 후보만 자동 제안
3. **최적 배정** `hungarian.py` — 대기 화물 전체 × 빈 기사 전체를 헝가리안 알고리즘으로 1:1 배정
   → 배정된 기사에게 제안, 거절 · 시간 초과면 다시 배정 (Redis 분산 락 안에서 실행)

### 주문 상태 FSM · 감사 로그 (SHA-256 해시 체인)
`engines/fsm/order_fsm.py` 의 전이표로만 상태가 바뀝니다. 모든 전이 · 관제 조치는 감사 로그에 남고,
각 줄은 직전 줄의 해시를 이어 SHA-256 으로 묶입니다 (`/admin/audit/verify` 위변조 검사).
```
화물  MATCHING → ASSIGNED → IN_TRANSIT → DELIVERED ⇄ SETTLED
청구  UNBILLED → REQUESTED → PAID → REFUNDED → (재청구) REQUESTED
```

### 명세 Phase 0~8 대응 (4차)
| Phase | 내용 | 주요 파일 |
|---|---|---|
| 0 | 사업자 Mod-10 · 국세청 API(선택) · 노란 번호판 · 필수 서류 3종 · prod compose | `engines/compliance_engine.py`, `docker-compose.prod.yml` |
| 1 | 안전율 90/95%(경고/차단) · 축중량(산안법 173조) · 필수 최소 톤수 · 날씨/수급 서지 · 포아송 주문 | `engines/validation_engine.py`, `engines/pricing_engine.py`, `simulator/virtual_shipper.py` |
| 2 | 하드 필터 · cf_score · LightGBM 10피처 · 0.75 임계값 · 매칭 메트릭스 | `engines/matcher/cf_score.py`, `models/metrics.py` |
| 3 | GPS 핑 API · 재위탁 방지 · Shapely 이탈 3회 디바운싱 · 지연 감지 · 해시 체인 | `engines/tracking_engine.py`, `services/tracking_service.py`, `models/audit.py` |
| 4 | 상호 리뷰(별점 · 태그) · 자동/수동 블랙리스트 | `services/review_service.py`, `models/review.py` |
| 5 | Gemini Function Calling 관제 (키 없으면 규칙) · 인시던트 리포트 · 조절 가이드 | `engines/ai/gemini_agent.py`, `engines/ai/rule_agent.py` |
| 6 | 마이크로 배치 스트레스 · GPS 오탐 검증 | `tests/stress/stress_test.py` |
| 8 | 벤치마킹 보고서 | `docs/설명/벤치마킹_보고서.md` |

---

## 폴더 구조

```
수정본/  (b2b-logistics-saas)
├── .github/workflows/ci.yml   # CI: pyflakes · 점검 스크립트 (3.12/3.14) + docker compose 통합 점검
├── .env.example / .env        # 환경변수 템플릿 / 로컬 실제 값 (.env 는 git 제외)
├── docker-compose.yml         # db · redis · backend · worker · simulator · frontend
├── docker-compose.prod.yml    # 운영용 (포트 비공개 · 자동 재시작 · 헬스체크 · 시뮬레이터는 --profile sim)
├── Dockerfile                 # 백엔드/워커/화면 공용 이미지 (Python 3.14)
├── requirements.txt           # FastAPI · Streamlit · SQLAlchemy · PostGIS · Redis · Celery · LightGBM …
├── backend/
│   ├── main.py                # 앱 진입점 (CORS · 요청 후 DB 저장 · GPS 워커 시작 · /m 모바일 웹)
│   ├── config.py              # pydantic-settings (.env)
│   ├── core/                  # database.py(연결 · 저장소 · DB 동기화) · security.py · redis_client.py · seed.py · errors.py
│   ├── models/                # user.py · order.py(orders · anomalies) · audit.py(해시 체인) · review.py · metrics.py
│   ├── schemas/               # auth · order · settlement · matching(핑 · 메트릭스) · review
│   ├── services/              # auth · order · matching · billing(AI 운임 · 수급 · MockPG) · tracking · review
│   ├── engines/
│   │   ├── packer/            # 3D 적재: rules.py(적재 규칙 R0~R10) · engine.py(규칙 + EP) · validate.py(규칙 재채점)
│   │   ├── matcher/           # spatial.py(PostGIS) · cf_score.py · ml_ranker.py(LightGBM) · hungarian.py
│   │   ├── compliance_engine.py · validation_engine.py · pricing_engine.py · tracking_engine.py
│   │   ├── fsm/order_fsm.py   # 상태 전이 · 감사 로그 해시 체인
│   │   └── ai/                # rule_agent.py(규칙) · gemini_agent.py(Gemini Function Calling)
│   ├── workers/               # celery_app.py · packing_tasks.py · gps_stream_worker.py
│   ├── api/                   # v1.py · endpoints/(auth · orders · matching · tracking · settlement · reviews)
│   └── data/                  # route_cache.json(도로 경로 990개) · ml_ranker.txt/.json(학습된 모델)
├── frontend/
│   ├── app.py                 # 로그인 · 역할별 메뉴
│   ├── pages/                 # 0_auth · 1_shipper_portal · 2_control_tower · 3_admin_compliance · 4_driver_app
│   ├── reviews_ui.py          # 화주 · 차주 상호 평가 패널
│   └── static/style.css       # 전역 스타일
├── mobile_web/                # index.html · js/load_visualizer.js · css/mobile_style.css (Three.js r147 CDN)
├── simulator/                 # virtual_driver.py(GPS) · virtual_shipper.py(가상 주문 · 부하 테스트)
├── docs/
│   ├── 가이드/                 # 실행방법 · 브랜치_전략
│   ├── 명세/                   # 기능명세서 · 컬럼명_리스트
│   ├── 설명/                   # 주요코드_설명 · 벤치마킹_보고서 · benchmark_metrics.json
│   └── internal/              # 발표자료 · 변경점 정리 (git 제외 · 내부용)
└── tests/                     # test_suites.py(pytest) · test_packing · test_matching · test_fsm · test_compliance ·
                               # test_pricing · test_tracking · stress_test · smoke_test
```

## 화면

| 역할 | 메뉴 |
|---|---|
| 화주 | 배차 · 실시간 관제 / 신규 화물 등록(운송 정보 → 적재 품목 → 3D 시뮬레이션(안전율 · 축중량) → AI 운임(날씨 · 수급 서지)) / 정산 · 운송 이력(모의 PG 결제 · 월별 인보이스 · 기사 평가) |
| 차주 | 배차 제안 · AI 추천 리스트 · 내 운행(평점 · 번호판 · 제한 상태) / 수익 · 화주 청구 · 화주 평가 / 휴대폰 3D 적재 가이드(과적 · 편심 경고) |
| 관제 | 전국 관제 지도(AI 명령창 · 브리핑 · 실시간 추적 · 권역 수급) / 매칭 분석(엔진 · 성능 로그 · 조절 가이드) / 이상 감지(AI 인시던트 리포트) / 컴플라이언스(가입 심사 · 리뷰/블랙리스트 · 해시 체인 감사 로그 · 결제 취소) / 데모 데이터 초기화 |

## 보안 설정
| 항목 | 개발용 `docker-compose.yml` | 운영용 `docker-compose.prod.yml` |
|---|---|---|
| 실행 계정 | root 아닌 `appuser`(UID 10001) · 코드는 읽기 전용 | 같음 + 리눅스 권한(capability) 전부 제거 |
| DB · Redis | `127.0.0.1` 에만 열림 (이 PC 의 DB 도구만 접속) | 포트 없음 · 내부 전용 `data` 망 |
| 네트워크 | 기본 망 | `data`(내부 전용: db · redis · backend · worker) / `edge`(backend · frontend · simulator) |
| 자원 | 제한 없음 | 서비스별 CPU · 메모리 상한 |
| 기타 | `no-new-privileges` · 재부팅 후 자동 재시작 | 같음 |
| 이미지 | `.dockerignore` 로 `.env*` · 문서 · CI · 캐시 제외 | 같음 |

남은 권장 사항: 인터넷에 공개하기 전 데모 비밀번호 · `ADMIN_APPROVAL_CODE` · `SIM_API_KEY` 교체, HTTPS 프록시, Redis 비밀번호.

## 브랜치 전략
`main`(운영) ← `staging`(통합 검증 · 시연) ← `feature/*` · `fix/*` (Pull Request + CI 통과 후 머지), 긴급은 `hotfix/*`.
CI 는 `main` · `staging` push 와 모든 PR 에서 실행. 자세한 규칙 · 배포 순서는 **[docs/가이드/브랜치_전략.md](docs/가이드/브랜치_전략.md)**.

## 테스트

```bash
docker compose exec backend python -m pytest      # 컨테이너 안 (pytest 설치 필요 시: pip install pytest)
python -m pytest                                  # 로컬 (requirements-dev.txt 설치 후)
```
점검 스크립트는 실제 DB 를 쓰지 않습니다(메모리 저장소). `smoke_test.py` 는 실행 중인 서버에 API 로 한 바퀴 돕니다.

## 저장 방식
메모리 저장소(Store)를 작업 사본으로 쓰고, 요청이 끝날 때마다 **바뀐 행만 PostgreSQL 에 UPSERT** 합니다.
서버를 재시작하면 DB 에서 다시 불러옵니다. DB 에 연결할 수 없으면 메모리만으로 동작합니다(재시작 시 초기화).
