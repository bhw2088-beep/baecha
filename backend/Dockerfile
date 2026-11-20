# 배차의 민족 — 백엔드 이미지 (FastAPI API 서버 + Celery 워커 공용)
# 실행 명령은 docker-compose.yml 에서 서비스별로 나눈다 (backend: uvicorn · worker: celery).
#
# 빌드 컨텍스트는 저장소 최상위다 (docker-compose.yml: context: . · dockerfile: backend/Dockerfile).
#   → 차주용 모바일 웹(frontend/mobile)을 백엔드가 /m 으로 같이 서비스하기 때문 (같은 주소에서 /api/v1 호출).
#
# 보안: 컨테이너는 root 가 아닌 전용 계정(appuser, UID 10001)으로 실행한다.
#       코드는 root 소유(읽기 전용), 쓰기가 필요한 src/data(모델 재학습 · 인수증 사진)만 appuser 소유.
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MOBILE_DIR=/app/mobile

WORKDIR /app

# LightGBM 이 쓰는 OpenMP 런타임
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# 실행 전용 계정 (로그인 셸 없음)
RUN groupadd --system --gid 10001 appuser \
    && useradd --system --uid 10001 --gid appuser --create-home --home-dir /home/appuser \
       --shell /usr/sbin/nologin appuser

# 라이브러리 먼저 설치 (코드만 바뀌면 이 단계는 캐시를 재사용)
COPY backend/requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# 서버 코드 (root 소유 → 실행 계정은 읽기만)
COPY backend/src ./src
COPY frontend/mobile ./mobile
RUN mkdir -p src/data/pod_photos && chown -R appuser:appuser src/data

USER appuser

EXPOSE 8000
CMD ["python", "-m", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
