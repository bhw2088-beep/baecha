# tests — 테스트 모음 (용도별 폴더)

VS Code 에서 파일을 열고 오른쪽 위 ▷ (Run Python File) 를 누르면 바로 실행된다.
프로젝트 라이브러리가 없는 파이썬(컴퓨터 기본 파이썬)이어도 됨 → 도커 이미지 `baecha-app:dev` 안에서 대신 실행된다 (Docker Desktop 필요).
터미널에서는 **수정본 폴더에서** `python tests/<폴더>/<파일>.py` 처럼 실행한다.

```
tests/
├─ README.md          이 안내
├─ _check.py          공용: PASS/FAIL 집계 · 라이브러리 없으면 도커로 자동 실행
├─ conftest.py        pytest 공통 설정
├─ test_suites.py     pytest 진입점 (CI 가 실행) → 아래 ★ 표시 스크립트를 차례로 실행
│
├─ basic/             기본 점검 · 서버 없이 · 각 수 초
├─ checks_6/          추가 검증 6가지 · run_all.py 로 한 번에
├─ stress/            부하 · 대량 테스트
├─ server/            실행 중인 서버가 필요한 점검
└─ metrics/           발표용 수치 산출
```

각 폴더의 `_common.py` 는 위 `_check.py` 를 불러오는 연결 파일이다 (직접 실행하지 않음).

## basic/ — 기본 점검 (서버 없이 · 수 초)

| 파일 | 점검 내용 | pytest |
|---|---|---|
| `test_packing.py` | 3D 적재 엔진 기본 동작 | ★ |
| `test_pricing.py` | 운임 · 수급(서지) · 적재 안전 규칙 | ★ |
| `test_matching.py` | 자동 매칭 · 도로 거리 · 운임 | ★ |
| `test_fsm.py` | 주문 상태 FSM · 감사 로그 | ★ |
| `test_compliance.py` | 가입 컴플라이언스 (사업자 확인 · 중복 가입 방지) | ★ |
| `test_tracking.py` | 실시간 추적 · 해시 체인 · 리뷰 | ★ |

## checks_6/ — 추가 검증 6가지

`run_all.py` 를 ▷ 하면 1~6번을 차례로 실행하고 **결과를 run_all.py 맨 아래에 저장**한다 (약 8~12분).
골라서: `python tests/checks_6/run_all.py --only 3,4,5` · 준비 확인만: `--check`

| 파일 | 점검 내용 | 시간 | pytest |
|---|---|---|---|
| `1_concurrency.py` | 동시성: 중복 배차 · 중복 결제 · 중복 화물 등록(Idempotency-Key) — 테스트 전용 스택을 띄우고 지움 | 2~3분 | |
| `2_redis_recovery.py` | Redis 장애 복구: GPS 스트림 미처리 메시지 · Celery 작업 재실행 — 테스트 전용 Redis | 1~2분 | |
| `3_packing_rules.py` | 적재 규칙 R0~R10 경계값 · 엔진 · 검증기 | 수 초 | ★ |
| `4_matching_ai.py` | 매칭 AI: LightGBM 시드 5개 · 임계값 0.75 · 재현성 · 단조 제약 · 매칭 연동 | 약 10초 | ★ |
| `5_ai_control.py` | AI 관제: 함수 권한 · 프롬프트 인젝션 · 전화번호 가림 · 장애 대체 (실제 Gemini 호출 없음) | 수 초 | ★ |
| `6_static_checks.py` | 정적 분석 · pytest: pyflakes · Ruff · Bandit · pip-audit · pytest (CI 와 같음, 도커에서) | 2~3분 | |

도우미: `_concurrency_compose.yml` (1번 전용) · `_celery_probe.py` (2번 전용)

## stress/ — 부하 · 대량

| 파일 | 내용 | pytest |
|---|---|---|
| `stress_test.py` | 마이크로 배치 매칭 스트레스 · GPS 디바운싱 (`--quick` 은 CI 용) | ★ (`--quick`) |
| `packing_stress.py` | 적재 전용 대량 계산 본체 (보통 `stress_8k/5_packing_stress.py` 가 실행) | |
| `stress_8k/` | 주문 8,000건 이상 스트레스 모음 · 결과는 `stress_8k/results/` — 자세한 내용은 [stress_8k/README.md](stress/stress_8k/README.md) | |

## server/ — 실행 중인 서버 필요

| 파일 | 내용 |
|---|---|
| `smoke_test.py` | API 전체 흐름 (등록 → 매칭 → 운행 → 청구 → 모의 결제 · Celery · 핑 · 리뷰 · 해시 체인). CI 통합 단계에서 실행. 서버가 없으면 pytest 에서 건너뜀 |

## metrics/ — 발표용 수치

| 파일 | 내용 |
|---|---|
| `eval_matching_lgbm.py` | 매칭 AI 성능 지표 (ROC-AUC · PR-AUC · LogLoss · 임계값 0.75 의 Precision/Recall/F1) · 결과는 파일 맨 아래에 저장 |

## pytest · CI

- `python -m pytest -q` (수정본 폴더) → `test_suites.py` 가 ★ 스크립트를 실행한다. 서버가 없으면 `server/smoke_test.py` 는 건너뜀.
- CI(`.github/workflows/ci.yml`): pyflakes · Ruff · Bandit · pip-audit → pytest → 통합 단계에서 `server/smoke_test.py`.
