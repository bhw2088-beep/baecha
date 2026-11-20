"""상태 배지(pill) · 지도 범례."""

from html import escape

from utils.theme import KIND_LABEL, MAP_COLORS, STATUS_TONE, TONES


def pill(text: str, tone: str = "gray", dot: bool = True) -> str:
    fg, bg = TONES.get(tone, TONES["gray"])
    return f'<span class="ct-pill" style="color:{fg};background:{bg}">{"<i></i>" if dot else ""}{escape(str(text))}</span>'


def status_pill(order: dict) -> str:
    if order.get("anomaly"):
        return pill(f"이상감지 · {order['anomaly']['text']}", "red")
    return pill(order["status_label"], STATUS_TONE.get(order["status"], "gray"))


def legend() -> str:
    return '<div class="ct-legend">' + "".join(
        f'<span><i style="background:rgb({",".join(map(str, MAP_COLORS[k]))})"></i>{v}</span>'
        for k, v in KIND_LABEL.items()
    ) + '<span><i style="background:transparent;box-shadow:inset 0 0 0 2px #0E1116"></i>등록 차주</span>' \
        '<span style="margin-left:auto">차량에 마우스를 올리면 상세 정보</span></div>'
