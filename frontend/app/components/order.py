"""화물 카드 조각: 상차→하차 진행 바 · 제시 운임 vs AI 적정 운임 비교."""

from html import escape

from utils.format import won

from .badges import pill


def route_bar(origin: str, destination: str, progress: float, left_meta: str = "", right_meta: str = "") -> str:
    p = max(0.0, min(100.0, progress or 0))
    return (f'<div class="ct-route"><div class="ends"><span><small>상차</small>{escape(origin)}</span>'
            f'<span><small>하차</small>{escape(destination)}</span></div>'
            f'<div class="ct-track"><div class="fill" style="width:{p}%"></div><div class="dot" style="left:{p}%"></div></div>'
            f'<div class="meta"><span>{escape(left_meta)}</span><span>{escape(right_meta)}</span></div></div>')


def fare_compare(fe: dict | None) -> str:
    """제시 운임 vs AI 적정 운임 (도로거리 · 차종 · 중량 기준) 한 줄 요약."""
    if not fe:
        return ""
    gap = fe["gap_ratio"]
    tone = "green" if gap >= 0.03 else "red" if gap <= -0.07 else "gray"
    word = (f"적정가보다 {gap * 100:.0f}% 높음" if gap >= 0.03 else
            f"적정가보다 {-gap * 100:.0f}% 낮음" if gap <= -0.03 else "적정가 수준")
    extra = " · ".join(f'{f["label"]} +{f["amount"]:,}' for f in fe["factors"])
    return (f'<div class="ct-fare"><span>AI 적정 운임 <b>{won(fe["recommended"])}</b> '
            f'<span class="ct-muted">· 도로 {fe["distance_km"]:.0f}km × {fe["per_km"]:,}원/km'
            f'{" · " + escape(extra) if extra else ""} · 제시 운임 km당 {fe["fare_per_km"]:,}원</span></span>'
            f'{pill(word, tone)}</div>')
