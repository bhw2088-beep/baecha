"""
초기(샘플) 데이터. 날짜는 '서버가 켜진 시각' 기준 상대값으로 만든다 → 언제 켜도 자연스럽게 보인다.

DB 를 붙이면 이 파일은 개발용 시드 스크립트로만 쓰면 된다.

시연 시나리오 (서버를 켜면 자동 매칭이 바로 돌아간다)
  - 평택 → 구미 냉동 화물  : 평택에 대기 중인 김철수(데모 기사, 11톤 냉동탑차)에게 배차 제안
  - 동탄 → 아산 장비 화물  : 동탄에 있는 강태풍(11톤 탑차)에게 제안 → 응답이 없으면 다음 순위로
  - 오송 → 서울 의약품     : 반경 50km 안에 냉장 공차가 없어 '매칭 실패' → 관제 화면에 노출
  - 평택 → 원주 냉동식품   : 김철수가 이미 거절한 화물 → 후보 소진으로 '매칭 실패'
  - 인천항 · 대전 출발 화물 : 반경 밖이라 자동 제안은 없지만 김철수의 'AI 추천 리스트'에서 직접 수락 가능
  - 삼성물류 운행 중 차량 12대 (기사 D-009~D-016 은 모두 운행 중 → 자동 매칭 후보에 걸리지 않음)
"""

from datetime import datetime, timedelta

DEMO_PASSWORD = "baecha1234"

SHIPPERS = [
    {"id": "S-001", "name": "삼성물류", "biz_no": "124-81-00998"},
    {"id": "S-002", "name": "현대글로비스", "biz_no": "211-81-36001"},
    {"id": "S-003", "name": "LG디스플레이", "biz_no": "107-81-46711"},
]

# 차주(기사). location = 공차 대기 위치(주소 키워드)
DRIVERS = [
    {"id": "D-001", "name": "김철수", "phone": "010-1234-1234", "plate": "경기 88바 7712", "truck_type": "11톤 냉동탑차",
     "location": "경기 평택항 물류단지", "month_runs": 42},
    {"id": "D-002", "name": "강태풍", "phone": "010-1234-1234", "plate": "충남 82바 1928", "truck_type": "11톤 탑차",
     "location": "경기 화성시 동탄", "month_runs": 38},
    {"id": "D-003", "name": "윤서준", "phone": "010-1234-1234", "plate": "경북 90아 3311", "truck_type": "25톤 무진동",
     "location": "울산 남구 여천동", "month_runs": 31},
    {"id": "D-004", "name": "한상우", "phone": "010-1234-1234", "plate": "충북 71자 4482", "truck_type": "5톤 윙바디",
     "location": "충북 청주시 오창", "month_runs": 29},
    {"id": "D-005", "name": "이민수", "phone": "010-1234-1234", "plate": "경북 80바 4920", "truck_type": "11톤 냉동탑차",
     "location": "인천 연수구 송도동", "month_runs": 27},
    {"id": "D-006", "name": "박성호", "phone": "010-1234-1234", "plate": "서울 98사 1102", "truck_type": "5톤 윙바디",
     "location": "서울 금천구 가산동", "month_runs": 25},
    {"id": "D-007", "name": "최동현", "phone": "010-1234-1234", "plate": "부산 72자 9081", "truck_type": "11톤 냉동탑차",
     "location": "경기 평택항 물류단지", "month_runs": 26},
    {"id": "D-008", "name": "정우진", "phone": "010-1234-1234", "plate": "부산 11가 2233", "truck_type": "1톤 카고",
     "location": "부산항", "month_runs": 22},
    # ---- 아래는 모두 운행 중인 기사 (화주 '진행 중 화물' · 관제 지도 시연용, 자동 매칭 후보에는 걸리지 않음) ----
    {"id": "D-009", "name": "장민호", "phone": "010-1234-1234", "plate": "경기 85사 2210", "truck_type": "11톤 탑차",
     "location": "경기 평택시 포승읍", "month_runs": 33},
    {"id": "D-010", "name": "서지훈", "phone": "010-1234-1234", "plate": "광주 81아 5532", "truck_type": "5톤 윙바디",
     "location": "광주 북구 용봉동", "month_runs": 28},
    {"id": "D-011", "name": "배준영", "phone": "010-1234-1234", "plate": "부산 83바 7741", "truck_type": "5톤 냉동탑차",
     "location": "부산 사상구 감전동", "month_runs": 30},
    {"id": "D-012", "name": "임도윤", "phone": "010-1234-1234", "plate": "충남 87자 3305", "truck_type": "11톤 탑차",
     "location": "충남 아산시 탕정면", "month_runs": 35},
    {"id": "D-013", "name": "권태양", "phone": "010-1234-1234", "plate": "충북 92사 6618", "truck_type": "25톤 무진동",
     "location": "충북 청주시 오창", "month_runs": 19},
    {"id": "D-014", "name": "노승현", "phone": "010-1234-1234", "plate": "서울 84아 1093", "truck_type": "1톤 카고",
     "location": "서울 종로구", "month_runs": 41},
    {"id": "D-015", "name": "하지민", "phone": "010-1234-1234", "plate": "울산 86바 4427", "truck_type": "11톤 탑차",
     "location": "울산 북구 양정동", "month_runs": 31},
    {"id": "D-016", "name": "문경훈", "phone": "010-1234-1234", "plate": "부산 89자 8810", "truck_type": "11톤 냉동탑차",
     "location": "부산항", "month_runs": 29},
]

USERS = [
    {"email": "logistics@samsung.com", "role": "shipper", "name": "삼성물류 담당자", "phone": "010-1234-1234",
     "org_id": "S-001", "sub": "화물 배차 및 관제팀"},
    {"email": "driver.kim@baecha.co.kr", "role": "driver", "name": "김철수", "phone": "010-1234-1234",
     "org_id": "D-001", "sub": "11톤 냉동탑차 · 경기 88바 7712"},
    {"email": "admin.control@baecha.co.kr", "role": "admin", "name": "전국 관제 총괄", "phone": "010-1234-1234",
     "org_id": None, "sub": "중앙 관제 센터"},
]

# 시드 전용 품목 목록 (samples.py 의 시나리오로 맞지 않는 5톤 냉장 화물용). 단위 mm / kg
PHARMA_ITEMS = [
    dict(name="의약품 냉장 박스", length=600, width=400, height=400, weight=15, qty=40,
         upright=True, stackable=True, max_top_load=100, drop=1),
    dict(name="백신 콜드체인 팔레트", length=1100, width=1100, height=1200, weight=400, qty=2,
         upright=True, stackable=False, max_top_load=None, drop=1),
]
FROZEN_SMALL_ITEMS = [
    dict(name="냉동 팔레트", length=1100, width=1100, height=1400, weight=600, qty=4,
         upright=True, stackable=True, max_top_load=300, drop=1),
    dict(name="냉동 박스", length=600, width=400, height=300, weight=18, qty=30,
         upright=True, stackable=True, max_top_load=120, drop=1),
]


def orders(now: datetime) -> list[dict]:
    """match = 과거 매칭 기록(상차지까지 거리, 부피 적재율). 적재 계획이 있으면 적재율은 계획 값으로 덮어쓴다."""
    m = lambda **kw: timedelta(**kw)  # noqa: E731
    return [
        # ---- 진행중 화물 (삼성물류) ----  ※ 인덱스 0~2 는 아래 anomalies() 가 참조
        dict(title="전자부품 11톤 (콜드체인)", truck_type="11톤 냉동탑차", cold_chain=True,
             origin="인천 연수구 송도동", destination="경북 구미시 공단동", price=680_000,
             shipper_id="S-001", driver_id="D-005", status="IN_TRANSIT",
             created_at=now - m(hours=5), assigned_at=now - m(hours=4, minutes=56),
             progress=68.0, speed=88, temp_set=-18.0, temp_now=-11.2,
             packing_sample="콜드체인 냉동식품", match=dict(pickup_km=6.2)),
        dict(title="의류/잡화 5톤 윙바디", truck_type="5톤 윙바디",
             origin="서울 금천구 가산동", destination="부산 사상구 감전동", price=520_000,
             shipper_id="S-001", driver_id="D-006", status="IN_TRANSIT",
             created_at=now - m(hours=6), assigned_at=now - m(hours=5, minutes=55),
             progress=42.0, speed=0, delay_minutes=50, match=dict(pickup_km=3.8, volume_util=0.64)),
        dict(title="신선식품 11톤 (냉동)", truck_type="11톤 냉동탑차", cold_chain=True,
             origin="경기 평택항 물류단지", destination="광주 북구 용봉동", price=740_000,
             shipper_id="S-001", driver_id="D-007", status="IN_TRANSIT",
             created_at=now - m(hours=4), assigned_at=now - m(hours=3, minutes=57),
             progress=55.0, speed=79, temp_set=-20.0, temp_now=-20.1, deviated=True,
             packing_sample="콜드체인 냉동식품", match=dict(pickup_km=2.1)),
        dict(title="산업용 원자재 25톤", truck_type="25톤 무진동",
             origin="울산 남구 여천동", destination="충남 당진시 송악읍", price=950_000,
             shipper_id="S-001", driver_id="D-003", status="ASSIGNED",
             created_at=now - m(hours=2), assigned_at=now - m(hours=1, minutes=55),
             packing_sample="팔레트 혼적 (단일 하차지)", match=dict(pickup_km=4.5)),
        dict(title="반도체 소재 11톤", truck_type="11톤 탑차",
             origin="경기 평택시 포승읍", destination="경북 구미시 공단동", price=690_000,
             shipper_id="S-001", driver_id="D-009", status="IN_TRANSIT",
             created_at=now - m(hours=3), assigned_at=now - m(hours=2, minutes=56),
             progress=35.0, speed=84, packing_sample="팔레트 혼적 (단일 하차지)", match=dict(pickup_km=3.1)),
        dict(title="가전 완제품 5톤 윙바디", truck_type="5톤 윙바디",
             origin="광주 북구 용봉동", destination="대전 유성구 대덕연구단지", price=470_000,
             shipper_id="S-001", driver_id="D-010", status="IN_TRANSIT",
             created_at=now - m(hours=3, minutes=40), assigned_at=now - m(hours=3, minutes=36),
             progress=61.0, speed=86, match=dict(pickup_km=5.4, volume_util=0.72)),
        dict(title="냉장 식자재 5톤", truck_type="5톤 냉동탑차", cold_chain=True,
             origin="부산 사상구 감전동", destination="대구 북구 산격동", price=360_000,
             shipper_id="S-001", driver_id="D-011", status="IN_TRANSIT",
             created_at=now - m(hours=1, minutes=50), assigned_at=now - m(hours=1, minutes=47),
             progress=24.0, speed=79, temp_set=-2.0, temp_now=-2.1,
             packing_items=FROZEN_SMALL_ITEMS, match=dict(pickup_km=2.7)),
        dict(title="디스플레이 모듈 11톤", truck_type="11톤 탑차",
             origin="충남 아산시 탕정면", destination="경기 파주시 문산읍", price=640_000,
             shipper_id="S-001", driver_id="D-012", status="IN_TRANSIT",
             created_at=now - m(hours=4, minutes=20), assigned_at=now - m(hours=4, minutes=17),
             progress=78.0, speed=90, match=dict(pickup_km=1.9, volume_util=0.66)),
        dict(title="배터리 셀 25톤 (무진동)", truck_type="25톤 무진동",
             origin="충북 청주시 오창", destination="울산 남구 여천동", price=1_080_000,
             shipper_id="S-001", driver_id="D-013", status="IN_TRANSIT",
             created_at=now - m(hours=1, minutes=10), assigned_at=now - m(hours=1, minutes=6),
             progress=12.0, speed=81, packing_sample="팔레트 혼적 (단일 하차지)", match=dict(pickup_km=8.8)),
        dict(title="의류 원단 1톤", truck_type="1톤 카고",
             origin="서울 종로구 대학로", destination="인천 연수구 송도동", price=150_000,
             shipper_id="S-001", driver_id="D-014", status="ASSIGNED",
             created_at=now - m(minutes=40), assigned_at=now - m(minutes=37), match=dict(pickup_km=2.2, volume_util=0.55)),
        dict(title="자동차 부품 11톤 (2차)", truck_type="11톤 탑차",
             origin="울산 북구 양정동", destination="충남 당진시 송악읍", price=820_000,
             shipper_id="S-001", driver_id="D-015", status="IN_TRANSIT",
             created_at=now - m(hours=2, minutes=30), assigned_at=now - m(hours=2, minutes=26),
             progress=50.0, speed=87, match=dict(pickup_km=6.0, volume_util=0.71)),
        dict(title="수산물 11톤 (냉동)", truck_type="11톤 냉동탑차", cold_chain=True,
             origin="부산항", destination="서울 금천구 가산동", price=890_000,
             shipper_id="S-001", driver_id="D-016", status="IN_TRANSIT",
             created_at=now - m(hours=3, minutes=10), assigned_at=now - m(hours=3, minutes=7),
             progress=40.0, speed=83, temp_set=-20.0, temp_now=-19.8,
             packing_sample="콜드체인 냉동식품", match=dict(pickup_km=1.5)),

        # ---- 매칭 대기 (서버가 켜지면 자동 매칭이 바로 돈다) ----
        dict(title="전자부품 11톤 (콜드체인 필수)", truck_type="11톤 냉동탑차", cold_chain=True,
             origin="경기 평택항 물류센터", destination="경북 구미 국가산업단지", price=720_000,
             shipper_id="S-002", status="MATCHING", created_at=now - m(minutes=1),
             packing_sample="콜드체인 냉동식품"),
        dict(title="의료용 의약품 5톤 냉동", truck_type="5톤 냉동탑차", cold_chain=True, urgent=True,
             origin="충북 청주시 오송읍", destination="서울 종로구 대학로", price=480_000,
             shipper_id="S-003", status="MATCHING", created_at=now - m(minutes=2),
             packing_items=PHARMA_ITEMS),
        dict(title="반도체 정밀장비 11톤", truck_type="11톤 탑차",
             origin="경기 화성시 동탄", destination="충남 아산시 탕정", price=380_000,
             shipper_id="S-001", status="MATCHING", created_at=now - m(minutes=8, seconds=42),
             packing_sample="팔레트 혼적 (단일 하차지)"),
        dict(title="신선 냉동식품 5톤 탑차", truck_type="5톤 냉동탑차", cold_chain=True,
             origin="경기 평택항 물류센터", destination="강원 원주시 문막읍", price=380_000,
             shipper_id="S-001", status="MATCHING", created_at=now - m(minutes=12, seconds=10),
             packing_items=FROZEN_SMALL_ITEMS, declined=["D-001"],
             match_note="강원권 회차 물량 부족으로 기사 수락 기피"),
        # 반경 50km 밖이라 자동 제안은 안 가지만, 기사 'AI 추천 리스트'에서 직접 수락할 수 있는 화물
        dict(title="냉동 수산물 11톤 (인천항)", truck_type="11톤 냉동탑차", cold_chain=True,
             origin="인천항 물류센터", destination="대전 유성구 대덕연구단지", price=640_000,
             shipper_id="S-002", status="MATCHING", created_at=now - m(minutes=3),
             packing_sample="콜드체인 냉동식품"),
        dict(title="생활용품 팔레트 11톤", truck_type="11톤 탑차",
             origin="대전 대덕구 물류단지", destination="부산 사상구 감전동", price=610_000,
             shipper_id="S-003", status="MATCHING", created_at=now - m(minutes=4),
             packing_sample="팔레트 혼적 (단일 하차지)"),

        # ---- 완료 이력 ----
        dict(title="정밀 IT 기판 11톤 (콜드체인)", truck_type="11톤 냉동탑차", cold_chain=True,
             origin="경기 화성시 동탄", destination="충남 아산시 탕정", price=480_000,
             shipper_id="S-001", driver_id="D-002", status="SETTLED",
             created_at=now - m(days=18, hours=8), assigned_at=now - m(days=18, hours=7, minutes=56),
             delivered_at=now - m(days=18), billing="PAID", match=dict(pickup_km=1.2, volume_util=0.58)),
        dict(title="디스플레이 글래스 패널 25톤", truck_type="25톤 무진동",
             origin="경북 구미시 공단동", destination="경기 파주시 탄현면", price=1_150_000,
             shipper_id="S-001", driver_id="D-003", status="DELIVERED",
             created_at=now - m(days=19, hours=9), assigned_at=now - m(days=19, hours=8, minutes=53),
             delivered_at=now - m(days=19), billing="REQUESTED", toll_fee=42_000,
             match=dict(pickup_km=18.6, volume_util=0.77)),
        dict(title="배터리 모듈 부품 5톤", truck_type="5톤 윙바디",
             origin="충북 청주시 오창", destination="울산 울주군 삼남", price=620_000,
             shipper_id="S-001", driver_id="D-004", status="SETTLED",
             created_at=now - m(days=22, hours=7), assigned_at=now - m(days=22, hours=6, minutes=55),
             delivered_at=now - m(days=22), billing="PAID", match=dict(pickup_km=2.4, volume_util=0.81)),
        dict(title="정밀 IT 기판 11톤 (콜드체인)", truck_type="11톤 냉동탑차", cold_chain=True,
             origin="인천항 물류센터", destination="대전 유성구 대덕연구단지", price=680_000,
             shipper_id="S-001", driver_id="D-001", status="DELIVERED",
             created_at=now - m(days=17, hours=6), assigned_at=now - m(days=17, hours=5, minutes=57),
             delivered_at=now - m(days=17), billing="UNBILLED", extra_fee=50_000, toll_fee=20_000,
             match=dict(pickup_km=27.5, volume_util=0.69)),
        dict(title="자동차 부품 11톤", truck_type="11톤 탑차",
             origin="울산 북구 양정동", destination="경기 평택시 포승읍", price=850_000,
             shipper_id="S-002", driver_id="D-001", status="DELIVERED",
             created_at=now - m(days=21, hours=9), assigned_at=now - m(days=21, hours=8, minutes=52),
             delivered_at=now - m(days=21), billing="REQUESTED", toll_fee=35_000,
             match=dict(pickup_km=12.9, volume_util=0.74)),
        dict(title="디스플레이 부품 11톤", truck_type="11톤 탑차",
             origin="경기 파주시 문산읍", destination="충남 아산시 탕정면", price=720_000,
             shipper_id="S-003", driver_id="D-001", status="SETTLED",
             created_at=now - m(days=24, hours=7), assigned_at=now - m(days=24, hours=6, minutes=54),
             delivered_at=now - m(days=24), billing="PAID", extra_fee=30_000, toll_fee=15_000,
             paid_days=2.1, match=dict(pickup_km=9.3, volume_util=0.66)),
        dict(title="반도체 웨이퍼 박스 11톤 (콜드체인)", truck_type="11톤 냉동탑차", cold_chain=True,
             origin="경기 화성시 동탄", destination="경북 구미시 공단동", price=760_000,
             shipper_id="S-001", driver_id="D-001", status="SETTLED",
             created_at=now - m(days=47, hours=6), assigned_at=now - m(days=47, hours=5, minutes=58),
             delivered_at=now - m(days=47), billing="PAID", toll_fee=28_000, paid_days=2.7,
             match=dict(pickup_km=31.0, volume_util=0.62)),
    ]


def anomalies(now: datetime) -> list[dict]:
    # order_index: 위 orders() 리스트의 인덱스
    return [
        dict(order_index=0, type="COLD", severity="HIGH", status="UNSOLVED", at=now - timedelta(minutes=1),
             text="콜드체인 온도 상승 (+6.8°C)", detail="설정 -18.0°C → 현재 -11.2°C",
             location="영동고속도로 여주 JCT 부근"),
        dict(order_index=1, type="STAGNATION", severity="MEDIUM", status="UNSOLVED", at=now - timedelta(minutes=4),
             text="45분 장시간 비정상 정체", detail="속도 0 km/h · 도착 예정 +50분 지연",
             location="경부고속도로 안성 휴게소 인근"),
        dict(order_index=2, type="DEVIATION", severity="MEDIUM", status="UNSOLVED", at=now - timedelta(minutes=11),
             text="지정 고속도로 경로 이탈 (12km)", detail="지정 경로: 서해안선 → 현재: 국도 우회",
             location="대전 중구 서대전네거리 주변"),
        dict(order_index=None, driver_id="D-003", type="STAGNATION", severity="LOW", status="RESOLVED",
             at=now - timedelta(hours=1, minutes=43), text="상차지 입고 지연 (20분)", detail="상차 도크 대기",
             location="구미 국가산업단지 3공단"),
    ]
