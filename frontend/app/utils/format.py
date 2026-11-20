"""표 · 카드에 보여 줄 값 포맷터 (금액 · 시간 · 상태 · 평점). 화면 코드에서는 fmt.won(...) 처럼 쓴다."""

from datetime import datetime

from .theme import STATE_LABEL


def won(n: int | float | None) -> str:
    return "-" if n is None else f"{int(n):,}원"


def state_text(code: str | None) -> str:
    """상태 코드 → 한글 (없으면 '-', 모르는 코드는 그대로)."""
    return "-" if not code else STATE_LABEL.get(code, code)


def num(v: int | float | None, fmt: str = "{:,}") -> str:
    """표 칸용 숫자 → 글자. 값이 없으면 'None' 대신 '-'."""
    return "-" if v is None else fmt.format(v)


def rating_text(avg: float | None) -> str:
    """표 칸용 평점 (리뷰가 없으면 '-')."""
    return "-" if avg is None else f"★{avg:.1f}"


def table_height(n: int, max_rows: int = 15) -> int:
    """행 수에 맞춘 표 높이 (머리글 38px + 행 35px). max_rows 를 넘으면 표 안에서 스크롤."""
    return 38 + 35 * max(1, min(n, max_rows)) + 2


def manwon(n: int | float | None) -> str:
    """큰 금액 요약 (예: 1,942,000 → 194.2만)"""
    if n is None:
        return "-"
    return f"{n / 10_000:,.1f}만" if n >= 100_000 else f"{int(n):,}"


def duration(seconds: int | None) -> str:
    if seconds is None:
        return "-"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}시간 {m}분"
    return f"{m}분 {s:02d}초" if m else f"{s}초"


def minutes_text(minutes: int | None) -> str:
    if minutes is None:
        return "-"
    h, m = divmod(minutes, 60)
    return f"{h}시간 {m}분" if h else f"{m}분"


def dt_text(iso: str | None, fmt: str = "%m.%d %H:%M") -> str:
    return datetime.fromisoformat(iso).strftime(fmt) if iso else "-"


def ago(iso: str | None) -> str:
    if not iso:
        return "-"
    sec = (datetime.now() - datetime.fromisoformat(iso)).total_seconds()
    if sec < 60:
        return "방금 전"
    if sec < 3600:
        return f"{int(sec // 60)}분 전"
    if sec < 86400:
        return f"{int(sec // 3600)}시간 전"
    return f"{int(sec // 86400)}일 전"


def temp_text(v: float | None) -> str:
    return "-" if v is None else f"{v:+.1f}°C"


def stars(avg: float | None, count: int | None = None, empty: str = "평가 없음") -> str:
    """평점 → '★4.5 (2건)' (평가가 없으면 empty)."""
    if avg is None:
        return empty
    return f"★{avg:.1f}" + (f" ({count}건)" if count else "")


def star_bar(n: int | None) -> str:
    """별점 1~5 → '★★★★☆' (없으면 '평가 전')."""
    return "평가 전" if not n else "★" * n + "☆" * (5 - n)
