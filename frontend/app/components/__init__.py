"""
공용 UI 컴포넌트 모음. 화면(pages/)에서는  import components as ui  로 가져와  ui.pill(...) 처럼 쓴다.

  layout.py     html · page_header · kpis · section · stats · banner
  badges.py     pill · status_pill · legend
  order.py      route_bar · fare_compare
  maps.py       route_map · fleet_routes_map
  feedback.py   flash · show_flash · load · act
  pack_3d.py    3D 적재 그림 (plotly)            → from components import pack_3d
  loading_guide.py  차주용 상차 · 하차 순서 안내  → from components import loading_guide
  reviews.py    리뷰 대기 패널                    → from components import reviews

값 포맷터는 utils/format.py, 색 · 라벨 토큰은 utils/theme.py 에 있다.
"""

from .badges import legend, pill, status_pill
from .feedback import act, flash, load, show_flash
from .layout import banner, html, kpis, page_header, section, stats
from .maps import MAP_GLIDE_MS, fleet_routes_map, route_map
from .order import fare_compare, route_bar

__all__ = [
    "legend", "pill", "status_pill",
    "act", "flash", "load", "show_flash",
    "banner", "html", "kpis", "page_header", "section", "stats",
    "MAP_GLIDE_MS", "fleet_routes_map", "route_map",
    "fare_compare", "route_bar",
]
