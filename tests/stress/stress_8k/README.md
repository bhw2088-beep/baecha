# tests/stress/stress_8k — 주문 8,000건 이상 스트레스 테스트 모음

매칭 · 적재 KPI를 **주문 8,008건(하루 1,001건 × 8일 · 가상 기사 400명)** 으로 측정하는 실행 파일과 결과를 한 폴더에 모았습니다.
실제 DB · 서버는 건드리지 않고, 계산은 도커 이미지(`baecha-app:dev`) 안에서 합니다.

## 실행 (VS Code)

1. **Docker Desktop 을 켭니다.** (이미지가 없으면 수정본 폴더에서 `docker compose up --build -d` 한 번)
2. 아래 파일 중 하나를 열고 **오른쪽 위 ▷ (Run Python File)** 을 누릅니다.
3. 아래 터미널에 진행 상황이 나오고, 끝나면 결과 표가 출력됩니다.
4. 실행이 성공하면 같은 결과 표가 **그 파일 맨 아래 `''' '''` 블록**에 자동으로 저장됩니다 (문자열이라 코드로 실행되지 않음 · 다음 실행 때 새 결과로 교체).

| 파일 | 하는 일 | 시간 |
|---|---|---|
| [`1_kpi_8k.py`](1_kpi_8k.py) | **KPI 목표치 측정** — 시드 7 · 8,008건 → KPI 표 · 상세 · 입력 오차 반영 위험률 | 약 6~8분 |
| [`2_seeds_8k.py`](2_seeds_8k.py) | **시드별 비교** — 시드 1 · 2 · 3 · 4 · 5 · 11 · 42 각 8,008건 (4개씩 동시) → 시드별 표 + 평균 | 약 20~30분 |
| [`3_margin_compare.py`](3_margin_compare.py) | **상부 허용하중 여유 비교** — 여유 0% vs 10% 를 같은 8,008건으로 → 현실 위험률 · 적재율 · 매칭 지표 전후 비교 | 약 15분 |
| [`4_show_results.py`](4_show_results.py) | **결과 보기** — 실행 없이 저장된 결과를 모두 표로 (도커 꺼져 있어도 됨) | 몇 초 |
| [`5_packing_stress.py`](5_packing_stress.py) | **과적 · 적재 대량 테스트** — 매칭 없이 적재 엔진만 무작위 화물 10,000건 (위반 화물 15% · 6종) → 입력 오차 ±5 · −5~+10 · ±15% × 상부 허용하중 여유 0 · 5 · 10 · 15% 별 현실 위험률 · 적재율 · 차단률 | 약 30분 (`--cases 3000` 이면 약 9분) |

> 왼쪽 **실행 및 디버그(Ctrl+Shift+D)** 목록에서 `8k ① KPI 측정` 등을 골라 ▷ 를 눌러도 같습니다 (`.vscode/launch.json`).
> 터미널에서: `python tests/stress/stress_8k/1_kpi_8k.py` (수정본 폴더에서). 옵션은 각 파일 맨 위 설명 참고 (`--quick`, `--parallel` 등).

**▷ 를 눌렀는데 `did not find executable ... python.exe` 같은 오류가 나면** — 수정본의 `.venv` 가 지워진 파이썬을 가리키는 상태입니다.
`Ctrl+Shift+P` → **Python: Select Interpreter** → 설치된 **Python 3.12 (Global)** 을 고르세요. 이 실행 파일들은 표준 라이브러리만 써서 패키지 설치가 필요 없습니다.

## 결과 파일 (`results/`)

| 경로 | 내용 | 만드는 파일 |
|---|---|---|
| `benchmark_large.json` | KPI 기준 결과 (시드 7 · 8,008건) — 관제 `GET /admin/kpi` 는 `backend/data/` 의 사본을 읽음 | ① |
| `large_run/orders.csv` · `rounds.csv` | 주문별 · 배정 라운드별 원본 | ① |
| `seeds/seed<N>.json` | 다른 시드 결과 | ② |
| `seeds/summary.md` | KPI 표 · 시드별 표 (보고서에 붙여 넣기용) | ① ② ④ |
| `margin_compare/margin_00.json` · `margin_10.json` · `summary.md` | 상부 허용하중 여유 0% / 10% 비교 | ③ |
| `packing/packing_stress.json` · `summary.md` | 과적 · 적재 대량 테스트 집계 · 표 | ⑤ |
| `logs/` | 실행 로그 (커밋 안 함) | 전부 |

> ⑤ 는 8,008건 매칭 시뮬레이션과 별개입니다. 8,008건 실행의 적재 검증(2,200건)은 샘플 화물 3종만 반복하므로,
> 과적 · 적재 규칙은 ⑤ 에서 무작위 화물(팔레트 · 박스 · 드럼 · 장비 · 장척물 / 냉동 · 파손주의 · 적층 불가 · 다중 하차)로 따로 검증합니다.
> 계산 본체: [`tests/stress/packing_stress.py`](../tests/stress/packing_stress.py).

## 측정 조건

- 하루 1,001건 포아송 (러시아워) × 8일 · 가상 기사 400명 · 8건씩 배정 라운드 · 3시간 미배차 주문 만료
- 위반 화물 12% 주입 (과적 110~120% · 편심) → 등록 단계 차단 여부
- 입력 오차: 품목마다 신고 무게 대비 실제 −5 ~ +10% → 배차된 적재 계획을 실제 무게로 다시 검증
- 같은 시드면 실행 시각과 상관없이 같은 결과 (시뮬레이션 시계) — 배차 지연(초)만 PC 성능에 따라 다름

시뮬레이션 본체는 [`tests/stress/stress_test.py`](../tests/stress/stress_test.py) 이고, 여기 파일들은 실행 · 진행 표시 · 표 정리만 합니다
([`_kpi.py`](_kpi.py) · [`_margin.py`](_margin.py) 가 공통 코드).
