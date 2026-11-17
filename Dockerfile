# 배차의 민족 — 백엔드(FastAPI)와 웹 화면(Streamlit)이 함께 쓰는 이미지
# 실행은 docker-compose.yml 에서 서비스별 명령으로 나눠서 합니다.
#
# 보안: 컨테이너는 root 가 아닌 전용 계정(appuser, UID 10001)으로 실행한다.
#       코드는 root 소유(읽기 전용), 쓰기가 필요한 backend/data(모델 재학습 · 인수증 사진)만 appuser 소유.
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# LightGBM 이 쓰는 OpenMP 런타임
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# 실행 전용 계정 (로그인 셸 없음 · 홈은 Streamlit 설정 · 캐시용)
RUN groupadd --system --gid 10001 appuser \
    && useradd --system --uid 10001 --gid appuser --create-home --home-dir /home/appuser \
       --shell /usr/sbin/nologin appuser

# 라이브러리 먼저 설치 (코드만 바뀌면 이 단계는 캐시를 재사용)
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# 프로젝트 코드 (root 소유 → 실행 계정은 읽기만)
COPY . .
RUN mkdir -p backend/data/pod_photos && chown -R appuser:appuser backend/data

USER appuser

EXPOSE 8000 8501
