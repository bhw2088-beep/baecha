"""
Streamlit 기본 페이지(index.html)의 언어를 한국어로 바꾸고 자동 번역을 끈다 (도커 이미지 빌드 때 1회 실행).

Streamlit 은 <html lang="en"> 으로 페이지를 내보내서, 크롬 · 엣지가 한국어 화면을 '영어'로 착각하고
"한국어로 번역"을 하면서 문장이 망가진다 (예: 로그인 → 입니다, 냉동탑차 → 물류탑차).
→ lang="ko" · translate="no" · <meta name="google" content="notranslate"> 를 넣어 번역 제안 자체를 막는다.

실행 (frontend 폴더에서): python scripts/patch_streamlit_lang.py   (로컬 가상환경에서도 같은 명령으로 적용 가능)
"""

import os
import re

import streamlit

path = os.path.join(os.path.dirname(streamlit.__file__), "static", "index.html")
with open(path, encoding="utf-8") as f:
    html = f.read()
before = html
html = re.sub(r"<html[^>]*>", '<html lang="ko" translate="no" class="notranslate">', html, count=1)
if 'name="google"' not in html:
    html = html.replace("<head>", '<head><meta name="google" content="notranslate" />', 1)
if html != before:
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
print(f"[patch] {path} → lang=ko · notranslate {'적용' if html != before else '이미 적용됨'}")
