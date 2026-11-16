"""신규 화물 등록 화면의 '샘플 품목 불러오기' 시나리오. 단위 mm / kg."""

SAMPLES = {
    "팔레트 혼적 (단일 하차지)": {
        "cold_chain": False,
        "items": [
            dict(name="전자부품 팔레트 (T11)", length=1100, width=1100, height=1200, weight=600, qty=8,
                 upright=True, stackable=True, max_top_load=650, drop=1),
            dict(name="정밀장비 크레이트", length=1500, width=1200, height=1400, weight=900, qty=1,
                 upright=True, stackable=False, max_top_load=None, drop=1),
            dict(name="부품 박스 (대)", length=600, width=400, height=400, weight=25, qty=24,
                 upright=False, stackable=True, max_top_load=150, drop=1),
            dict(name="소형 박스", length=400, width=300, height=250, weight=8, qty=30,
                 upright=False, stackable=True, max_top_load=60, drop=1),
        ],
    },
    "다중 하차 (3곳 경유)": {
        "cold_chain": False,
        "items": [
            dict(name="1차 하차 · 평택 팔레트", length=1100, width=1100, height=1300, weight=550, qty=4,
                 upright=True, stackable=True, max_top_load=500, drop=1),
            dict(name="2차 하차 · 천안 박스", length=800, width=600, height=500, weight=40, qty=16,
                 upright=False, stackable=True, max_top_load=200, drop=2),
            dict(name="3차 하차 · 대전 팔레트", length=1100, width=1100, height=1100, weight=700, qty=6,
                 upright=True, stackable=True, max_top_load=700, drop=3),
            dict(name="3차 하차 · 대전 유리 패널", length=1800, width=300, height=1200, weight=120, qty=3,
                 upright=True, stackable=False, max_top_load=None, drop=3),
        ],
    },
    "콜드체인 냉동식품": {
        "cold_chain": True,
        "items": [
            dict(name="냉동 팔레트", length=1100, width=1100, height=1400, weight=750, qty=6,
                 upright=True, stackable=True, max_top_load=300, drop=1),
            dict(name="냉동 박스", length=600, width=400, height=300, weight=18, qty=40,
                 upright=True, stackable=True, max_top_load=120, drop=1),
            dict(name="아이스박스 (파손주의)", length=500, width=350, height=350, weight=10, qty=12,
                 upright=True, stackable=False, max_top_load=None, drop=1),
        ],
    },
}
