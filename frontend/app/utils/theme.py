"""
디자인 토큰: 색상 · 상태별 톤 · 지도 색 · 한글 라벨 · 로고, 전역 CSS 주입.

색이나 라벨을 바꿀 때는 이 파일만 고친다. 화면 조각(컴포넌트)은 components/ 에 있다.
"""

import os

import streamlit as st

# ---------------------------------------------------------------- 색상 토큰
INK = "#0E1116"
MUTED = "#667085"
PAPER = "#F7F8FA"
LINE = "#E4E7EC"
BRAND = "#4F46E5"   # indigo

TONES = {  # 상태/의미별 색 (전경, 배경)
    "brand": ("#4338CA", "#EEF2FF"),
    "blue": ("#1D4ED8", "#EFF4FF"),
    "green": ("#067647", "#ECFDF3"),
    "teal": ("#0E7490", "#ECFEFF"),
    "amber": ("#B54708", "#FFFAEB"),
    "red": ("#B42318", "#FEF3F2"),
    "gray": ("#475467", "#F2F4F7"),
}
STATUS_TONE = {
    "MATCHING": "amber", "ASSIGNED": "gray", "IN_TRANSIT": "blue", "DELIVERED": "teal", "SETTLED": "green",
    "UNBILLED": "brand", "REQUESTED": "amber", "PAID": "green", "REFUNDED": "red",
    "UNSOLVED": "red", "RESOLVED": "green", "HIGH": "red", "MEDIUM": "amber", "LOW": "blue",
}
# 지도 색 [R,G,B]
MAP_COLORS = {
    "transit": [59, 130, 246], "cold": [20, 184, 166], "empty": [34, 197, 94], "anomaly": [239, 68, 68],
}
KIND_LABEL = {"transit": "운행중", "cold": "콜드체인", "empty": "공차 대기", "anomaly": "이상 감지"}
# 표에 영어 코드 대신 보여 줄 한글 (화물 · 청구 상태, 추적 기록, 심각도)
STATE_LABEL = {
    "MATCHING": "매칭중", "ASSIGNED": "배차", "IN_TRANSIT": "운행중", "DELIVERED": "하차완료", "SETTLED": "정산완료",
    "UNBILLED": "청구 대기", "REQUESTED": "청구됨", "PAID": "결제완료", "REFUNDED": "환불",
    "TRANSFERRED": "송금완료",
    "DEVIATED": "경로 이탈", "SUSPICIOUS": "재위탁 의심",
    "COLD": "온도 이상", "STAGNATION": "장시간 정체", "DEVIATION": "경로 이탈",
    "UNSOLVED": "미해결", "RESOLVED": "조치완료",
    "PENDING": "승인 대기", "APPROVED": "승인", "REJECTED": "반려",
    "AUTO_PASS": "자동 통과", "AUTO_REJECT": "자동 차단", "BLACKLISTED": "이용 제한", "ACTIVE": "정상",
}
SEVERITY_LABEL = {"HIGH": "긴급", "MEDIUM": "주의", "LOW": "참고"}
LOGO = '<span class="ct-logo"><span class="ct-mark"></span>배차의 민족</span>'


# 전역 CSS 는 app/static/style.css 에 있다 (정적 리소스)
STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
with open(os.path.join(STATIC_DIR, "style.css"), encoding="utf-8") as _f:
    CSS = "<style>" + _f.read() + "</style>"


def inject_css():
    st.html(CSS)
