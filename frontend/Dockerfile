# 배차의 민족 — 웹 화면 이미지 (Streamlit)
# 빌드 컨텍스트: frontend 폴더 (docker-compose.yml: context: ./frontend)
# 화면은 백엔드 API(BAECHA_API_BASE)만 호출한다. DB · Redis 에는 접속하지 않는다.
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 실행 전용 계정 (로그인 셸 없음 · 홈은 Streamlit 설정 · 캐시용)
RUN groupadd --system --gid 10001 appuser \
    && useradd --system --uid 10001 --gid appuser --create-home --home-dir /home/appuser \
       --shell /usr/sbin/nologin appuser

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# 화면 언어를 한국어로 표시 · 브라우저 자동 번역 끄기 (번역 때문에 문장이 깨지는 문제 방지)
COPY scripts ./scripts
RUN python scripts/patch_streamlit_lang.py

# 화면 코드 + Streamlit 설정 (.streamlit 은 실행 위치 /app 에서 읽는다)
COPY .streamlit ./.streamlit
COPY app ./app

USER appuser

EXPOSE 8501
CMD ["python", "-m", "streamlit", "run", "app/main.py", "--server.address", "0.0.0.0", "--server.port", "8501"]
