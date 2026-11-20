"""화면 뼈대 컴포넌트: HTML 래퍼 · 페이지 머리글 · KPI 카드 · 섹션 제목 · 통계 묶음 · 배너."""

from html import escape

import streamlit as st

from utils.theme import TONES


def html(markup: str):
    st.markdown(f'<div class="ct">{markup}</div>', unsafe_allow_html=True)


def page_header(eyebrow: str, title: str, desc: str = ""):
    html(f'<div class="ct-head"><div><div class="ct-eyebrow">{escape(eyebrow)}</div>'
         f'<div class="ct-title">{escape(title)}</div><div class="ct-desc">{escape(desc)}</div></div></div>')


def kpis(items: list[dict]):
    """items: [{label, value, unit?, note?, alert?}]"""
    cells = "".join(
        f'<div class="ct-kpi{" alert" if it.get("alert") else ""}"><div class="l">{escape(it["label"])}</div>'
        f'<div class="v">{escape(str(it["value"]))}<small>{escape(it.get("unit", ""))}</small></div>'
        f'<div class="n">{escape(it.get("note", ""))}</div></div>'
        for it in items
    )
    html(f'<div class="ct-kpis" style="--n:{len(items)}">{cells}</div>')


def section(title: str, aside: str = ""):
    html(f'<div class="ct-sec"><b>{escape(title)}</b><span>{escape(aside)}</span></div>')


def stats(items: list[tuple[str, str]]) -> str:
    return '<div class="ct-stats">' + "".join(
        f'<div class="ct-stat"><div class="l">{escape(l)}</div><div class="v">{escape(v)}</div></div>' for l, v in items
    ) + "</div>"


def banner(text: str, tone: str = "red") -> str:
    fg, bg = TONES[tone]
    return f'<div class="ct-banner" style="color:{fg};background:{bg}">{escape(text)}</div>'
