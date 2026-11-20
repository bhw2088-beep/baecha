# AGENTS.md — 배차의 민족 (b2b-logistics-saas)

AI 코딩 에이전트(Claude Code · Codex · Cursor 등)가 이 저장소에서 작업할 때 읽는 **단 하나의 프롬프트 파일**입니다.
`CLAUDE.md` 는 이 파일을 가리키기만 하므로, 규칙은 여기에만 적습니다.

## 프로젝트 한 줄 요약
화주가 화물을 등록하면 3D 적재 시뮬레이션으로 실리는 차량만 고르고,
PostGIS 반경 필터 → 규칙 점수 + LightGBM 수락 예측 → 헝가리안 배정으로 기사에게 배차를 제안하는 B2B 물류 SaaS.
화면은 화주 · 차주 · 통합관제 3가지 역할이고, 차주용 휴대폰 3D 적재 가이드가 따로 있습니다.

## 폴더 구조 (반드시 이 구조를 지킨다)
```
backend/            백엔드 서버 코드만 (FastAPI · Celery)
  src/
    main.py         앱 진입점
    config.py       설정 (.env)
    routers/        API 진입점 (/api/v1). 요청 검증 · 권한 확인 · 서비스 호출만, 로직은 넣지 않는다
    orm/            DB — database.py(연결 · 저장소) · seed.py · models/(SQLAlchemy 테이블)
    schemas/        요청 · 응답 pydantic 모델
    services/       기능 흐름 (주문 · 매칭 · 정산 · 추적 · 리뷰 · 인증)
    engines/        핵심 알고리즘 (packer · matcher · fsm · ai · pricing · tracking · compliance · validation · kpi)
    workers/        Celery 작업 · GPS 스트림 워커
    utils/          공용 함수 (errors · security · redis_client · perf)
    data/           학습된 모델 · 도로 경로 캐시 · KPI 기준값
  tests/
    unit_tests/     단위 테스트 (src 구조를 따라 engines/ · services/ · orm/ · utils/)
    e2e_tests/      API 전체 흐름 (기본 프로세스 안 TestClient · E2E_API_BASE 면 실행 중인 서버)
    stress_tests/   빠른 스트레스(pytest) + 8,000건 이상 대량 실행 파일
  Dockerfile        API · 워커 공용 이미지 (빌드 컨텍스트: 저장소 최상위)
frontend/           웹 화면 (Streamlit)
  app/
    main.py         진입점 · 역할별 메뉴
    pages/          화면 (페이지 함수)
    components/     공용 UI 컴포넌트 — 화면에서는  import components as ui
    utils/          api · session · nav · format(포맷터) · theme(디자인 토큰)
    static/         전역 CSS
  mobile/           차주용 휴대폰 3D 적재 가이드 (정적 HTML · 백엔드가 /m 으로 서비스)
  Dockerfile
simulator/          가상 기사 GPS · 가상 화주 주문 (시연 · 테스트용 모의 데이터) + Dockerfile
scripts/            부하 테스트 · 도로 경로 캐시 생성 등 일회성 도구
docs/               가이드 · 명세 · 설명 문서
```

### 어디에 무엇을 두나
- 새 API → `backend/src/routers/<도메인>.py` 에 라우트, 로직은 `services/`, 계산은 `engines/`.
- DB 테이블 → `backend/src/orm/models/`. 공용 헬퍼 → `backend/src/utils/`.
- 백엔드 import 는 항상 절대 경로 `from src.<패키지> import ...` (상대 import 금지).
- 화면에서 두 번 이상 쓰는 UI 조각은 `frontend/app/components/` 로 옮기고 `components/__init__.py` 에서 내보낸다.
  색 · 라벨은 `utils/theme.py`, 숫자 · 날짜 표시는 `utils/format.py` 만 쓴다 (화면 파일에 색 코드 · 포맷 로직을 새로 만들지 않는다).
- `backend/` 에는 서버 코드와 그 테스트만 둔다. 시뮬레이터 · 부하 도구는 `simulator/` · `scripts/`.

## 실행
```bash
copy .env.example .env              # 처음 한 번 (Windows) · macOS/Linux: cp
docker compose up --build -d        # db · redis · backend · worker · simulator · frontend
```
- 웹 http://localhost:8501 · API 문서 http://localhost:8000/docs · 모바일 가이드 http://localhost:8000/m/
- 로컬 실행: `cd backend && uvicorn src.main:app --reload --reload-dir src --port 8000`
  / `cd frontend && streamlit run app/main.py`

## 테스트 · 검사 (변경 후 반드시 실행)
```bash
cd backend
pip install -r requirements-dev.txt
python -m pytest -q                         # 전부 (단위 + e2e(프로세스 안) + 빠른 스트레스 · 약 1분)
python -m pytest tests/unit_tests -q        # 단위 테스트만 (약 25초) — 코드 고칠 때마다
python -m pytest -m "not stress" -q         # 스트레스 제외
python -m pytest tests/unit_tests/engines/test_packer.py -k actual   # 파일 하나 · 이름으로 고르기
cd ..
python -m pyflakes backend/src backend/tests frontend/app frontend/scripts simulator scripts
```
- 점검 스크립트는 `BAECHA_DB=0` 메모리 저장소를 쓴다. 실제 DB · 외부 API(국세청 · Gemini)를 부르지 않는다.
- 테스트는 pytest 함수로 쓴다. 단위 테스트는 `tests/unit_tests/<src 의 폴더>/test_<모듈>.py` 에 두고 이름을 이렇게 붙인다:
  - `test_ctor_*` — 생성자 · 기본값 · 상수 표가 의도대로인가
  - `test_<대상>_smoke` — 정상 입력에 오류 없이 기대한 형태(키 · 타입 · 범위)가 나오는가
  - `test_<대상>_actual_*` — 실제 규칙 · 경계값 · 실패 케이스 (숫자 · 문구까지 구체적으로 단언)
- 기능을 바꾸거나 고칠 때는 먼저 실패하는 `actual` 테스트를 쓰고(TDD), 고친 뒤 통과를 확인한다.
- 저장소(Store)가 필요하면 `store` fixture 를 쓴다 (시드 복사본 · 테스트끼리 영향 없음). 실제 DB · Redis 는 쓰지 않는다.
- e2e 는 `tests/e2e_tests/` — 기본은 프로세스 안 TestClient, `E2E_API_BASE=http://localhost:8000/api/v1` 이면 실행 중인 서버.
- Windows 에서 pip 가 requirements 의 한글 주석을 못 읽으면 `PYTHONUTF8=1` 을 켠다.

## 작업 규칙
- 브랜치: `main`(운영) ← `staging` ← `feature/*` · `fix/*`. 자세한 내용은 `docs/가이드/브랜치_전략.md`.
- 문서 · 주석 · 화면 문구는 한국어로 쓴다. 코드 식별자는 영어.
- `.env` · 비밀값은 커밋하지 않는다. 새 설정값은 `backend/src/config.py` 와 `.env.example` 에 같이 추가한다.
- 모의 데이터(GPS · 결제 · 사업자 확인 · 날씨)는 모의임이 화면 · 문서에 드러나야 한다.
- 폴더를 옮기거나 경로를 바꾸면 Dockerfile · docker-compose*.yml · CI(.github/workflows/ci.yml) · README 의 경로도 같이 고친다.
