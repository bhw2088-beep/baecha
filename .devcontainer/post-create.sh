#!/usr/bin/env bash
# Codespace 를 처음 만들 때 1회: .env 준비 (비밀값은 Codespaces Secrets 에서만 받아 넣는다)
set -e
if [ ! -f .env ]; then
  cp .env.example .env
  echo "[post-create] .env.example → .env 생성"
fi
for key in NTS_API_KEY GEMINI_API_KEY; do
  val="${!key}"
  if [ -n "$val" ]; then
    sed -i "/^${key}=/d" .env
    echo "${key}=${val}" >> .env
    echo "[post-create] Codespaces Secret ${key} 적용"
  fi
done
echo "[post-create] 준비 완료 — 잠시 뒤 docker compose 가 서비스를 띄웁니다 (처음 빌드 5~10분)"
