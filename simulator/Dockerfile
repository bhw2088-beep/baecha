# 배차의 민족 — 가상 기사 · 화주 시뮬레이터 이미지 (시연 · 테스트용 모의 데이터)
# 빌드 컨텍스트: 저장소 최상위 (docker-compose.yml: context: . · dockerfile: simulator/Dockerfile)
#   → 거점 좌표 · 차종 · 시간대 수요를 백엔드 엔진 정의(backend/src, 표준 라이브러리만 쓰는 모듈)에서 그대로 읽는다.
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN groupadd --system --gid 10001 appuser \
    && useradd --system --uid 10001 --gid appuser --no-create-home --shell /usr/sbin/nologin appuser

COPY simulator/requirements.txt ./simulator/requirements.txt
RUN pip install -r simulator/requirements.txt

COPY backend/src ./backend/src
COPY simulator ./simulator

USER appuser

# 가상 화주 주문:  docker compose run --rm simulator python -m simulator.virtual_shipper --count 5
CMD ["python", "-m", "simulator.virtual_driver"]
