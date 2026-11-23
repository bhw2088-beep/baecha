"""
운임 · 수급 · 적재 안전 규칙 점검 (서버 없이).   실행: 수정본 폴더에서  python tests/basic/test_pricing.py

- 모의 기상: 확률 분포(맑음 · 비 · 폭설), 같은 권역 · 시간대 재현성, 할증 배율 비 15% · 폭설 30%
- 수급 D/S: 러시아워 포아송 수요 폭증, 1.5 초과 시 서지 발동 · 상한 30%
- 운임 산정: 날씨 · 서지 요인이 기본 운임 × 배율로 결합
- 적재 안전율(90% 경고 · 95% 차단) · 축중량 균형(경량 화물 완화) · 필수 최소 톤수
- 매칭 점수: cf_score 범위 · 모의 스코어러 0~1 · 임계값 0.75
"""

import random
from datetime import datetime

from _common import check, finish, section

from backend.engines import pricing_engine as pe
from backend.engines import validation_engine as ve
from backend.engines.matcher import ml_ranker
from backend.engines.matcher.cf_score import cf_score
from backend.engines.packer.trucks import TRUCKS
from backend.services import billing_service as pricing

section("모의 기상 (확률 분포)")
at = datetime(2026, 11, 17, 14)
w1, w2 = pe.mock_get_weather_condition("서울", at), pe.mock_get_weather_condition("서울", at)
check("같은 권역 · 시간대 → 같은 날씨", w1["code"] == w2["code"])
check("할증 배율 비 15% · 폭설 30%", pe.WEATHER["RAIN"]["multiplier"] == 0.15 and pe.WEATHER["SNOW"]["multiplier"] == 0.30)
codes = [pe.mock_get_weather_condition(f"R{i}", at)["code"] for i in range(3000)]
share = {c: codes.count(c) / len(codes) for c in pe.WEATHER}
check("분포가 설정 확률과 비슷 (±4%p)", all(abs(share[c] - p) < 0.04 for c, p in pe.weather_distribution(11).items()),
      " · ".join(f"{pe.WEATHER[c]['label']} {share[c]:.0%}" for c in share))
check("겨울엔 폭설 확률 증가", pe.weather_distribution(1)["SNOW"] > pe.weather_distribution(7)["SNOW"])

section("수급 D/S · 서지")
rng = random.Random(1)
check("포아송 평균 ≈ λ", abs(sum(pe.poisson(6, rng) for _ in range(4000)) / 4000 - 6) < 0.3)
rush = sum(pe.market("경기", at=datetime(2026, 11, 17, 8, d))["ds_ratio"] for d in range(1, 29)) / 28
dawn = sum(pe.market("경기", at=datetime(2026, 11, d, 3))["ds_ratio"] for d in range(1, 29)) / 28
check("러시아워 D/S > 새벽 D/S", rush > dawn * 2, f"08시 평균 {rush:.2f} · 03시 평균 {dawn:.2f}")
check("D/S 1.5 이하 → 서지 없음", pe.surge_multiplier(1.5) == 0.0)
check("D/S 1.6 → 서지 발동", pe.surge_multiplier(1.6) > 0)
check("서지 상한 30%", pe.surge_multiplier(10) == 0.30)
check("러시아워 판정 08시 · 18시", pe.is_rush_hour(datetime(2026, 1, 1, 8)) and pe.is_rush_hour(datetime(2026, 1, 1, 18))
      and not pe.is_rush_hour(datetime(2026, 1, 1, 12)))

section("운임 산정에 날씨 · 서지 결합")
mk = {"region": "경기", "demand": 12, "supply": 4, "ds_ratio": 3.0, "surge": pe.surge_multiplier(3.0), "surge_on": True,
      "rush_hour": True, "real_demand": 0, "real_supply": 0,
      "weather": {"region": "경기", "code": "SNOW", "label": "폭설", "multiplier": 0.30}}
e = pricing.estimate("11톤 탑차", "경기 평택시", "부산항", market=mk)
f = {x["key"]: x["amount"] for x in e["factors"]}
check("폭설 할증 = 기본 운임 × 30%", abs(f["weather"] - e["base"] * 0.30) <= 5_000, f"{f['weather']:,}원")
check("서지 할증 = 기본 운임 × 25%", abs(f["surge"] - e["base"] * 0.25) <= 5_000, f"{f['surge']:,}원")
calm = dict(mk, ds_ratio=1.0, surge=0.0, surge_on=False, weather={**mk["weather"], "code": "CLEAR", "multiplier": 0.0})
check("평상시보다 비쌈", e["recommended"] > pricing.estimate("11톤 탑차", "경기 평택시", "부산항", market=calm)["recommended"])

section("적재 안전율 · 축중량 · 필수 톤수")
check("88% → 통과", ve.safety_check(0.88, 0.5)["status"] == "pass")
check("92% → 경고 (공간 부족 위험)", ve.safety_check(0.92, 0.5)["status"] == "warn")
check("97% → 차단 (안전율 5%)", ve.safety_check(0.5, 0.97)["status"] == "fail")
rows = [dict(x=0, y=0, z=0, length=1000, width=1000, height=1000, weight_kg=4000)]
check("앞쪽 · 한쪽에 몰린 무거운 화물 → 차단", ve.axle_balance(rows, 6000, 2400, 5000)["status"] == "fail")
light = [dict(x=0, y=0, z=0, length=1000, width=1000, height=1000, weight_kg=300)]
check("같은 위치라도 가벼우면 경고로 완화", ve.axle_balance(light, 6000, 2400, 5000)["status"] == "warn")
mid = [dict(x=2500, y=700, z=0, length=1000, width=1000, height=1000, weight_kg=4000)]
ab = ve.axle_balance(mid, 6000, 2400, 5000)
check("가운데 화물 → 통과 · 앞축+뒤축 = 총중량", ab["status"] == "pass" and abs(ab["front_kg"] + ab["rear_kg"] - 4000) < 1)
check("필수 최소 톤수: 950kg → 1톤 차량은 안전율 5% 한도(950kg) 안", ve.required_tonnage(950, 1e9, list(TRUCKS.values())) == 1.0)
check("필수 최소 톤수: 960kg → 2.5톤", ve.required_tonnage(960, 1e9, list(TRUCKS.values())) == 2.5)
check("냉장 화물은 냉장 차량 중에서", ve.required_tonnage(900, 1e9, list(TRUCKS.values()), cold_chain=True) == 5.0)

section("매칭 점수 피처 · 임계값")
check("cf_score 0~1 · 주 권역이면 높음", cf_score("경기 평택시", "경기 화성시", "부산항", [], []) >
      cf_score("부산항", "경기 화성시", "서울 중구", [], []))
check("거절 이력이 있으면 낮아짐", cf_score("경기 평택시", "경기 평택시", "부산항", [], ["경기"] * 3) <
      cf_score("경기 평택시", "경기 평택시", "부산항", [], []))
feat = dict(pickup_km=5, util=0.8, fare_gap=0.05, trip_km=200, cold=0, urgent=0, accept_rate=0.8, cf_score=0.8,
            rating=4.8, rush_hour=0)
far = dict(feat, pickup_km=55, cf_score=0.2, rating=2.5, rush_hour=1, fare_gap=-0.2)
check("모의 스코어러 0~1", all(0 <= ml_ranker.mock_lgb_score(x) <= 1 for x in (feat, far)))
check("가깝고 평점 좋으면 0.75 이상", ml_ranker.predict([feat])[0] >= 0.75, f"{ml_ranker.predict([feat])[0]:.3f}")
check("멀고 평점 낮고 러시아워면 0.75 미만", ml_ranker.predict([far])[0] < 0.75, f"{ml_ranker.predict([far])[0]:.3f}")
check("모델 피처 12개 (cf_score · rating · rush_hour · weather · ds_ratio)",
      ml_ranker.info().get("features") == ml_ranker.FEATURES and len(ml_ranker.FEATURES) == 12)
bad_weather = dict(feat, weather=0.30, ds_ratio=4.0)
check("폭설 · 수급 과열이면 수락 예측 하락", ml_ranker.predict([bad_weather])[0] < ml_ranker.predict([feat])[0])

section("통행료 · 오지 공차 · 장재물 · 예약")
from datetime import timedelta  # noqa: E402
t1, t5 = pe.mock_toll_lookup(1.0, 300), pe.mock_toll_lookup(25.0, 300)
check("통행료: 25톤(5종) > 1톤(1종)", t5["amount"] > t1["amount"] > 0, f"{t1['amount']:,}원 · {t5['amount']:,}원")
check("20km 미만은 통행료 0", pe.mock_toll_lookup(11.0, 15)["amount"] == 0)
check("강원 하차 → 오지 공차 +10%", pe.remote_surcharge("강원", 30)["rate"] == 0.10)
check("거점 50km 밖 → 오지 공차", pe.remote_surcharge("경북", 80)["rate"] == 0.10)
check("거점 인근 → 할증 없음", pe.remote_surcharge("경기", 10)["rate"] == 0.0)
check("장재물 6m → +10%", pe.long_cargo_surcharge(6200, 9100)["rate"] == 0.10)
check("장재물 9m 이상 → +20%", pe.long_cargo_surcharge(9500, 10100)["rate"] == 0.20)
check("짧은 품목 → 할증 없음", pe.long_cargo_surcharge(1200, 9100)["rate"] == 0.0)
now = datetime(2026, 11, 17, 10)
check("24시간 이전 예약 → 5% 할인", pe.reservation(now + timedelta(hours=30), now)["discount"] == 0.05)
check("곧 상차 예약 → 할인 없음", pe.reservation(now + timedelta(hours=3), now)["discount"] == 0.0)
check("지난 시각 → 즉시 배차", not pe.reservation(now - timedelta(hours=1), now)["reserved"])
e1 = pricing.estimate("11톤 탑차", "경기 평택시", "강원 원주시", market=calm)
f1 = {x["key"]: x["amount"] for x in e1["factors"]}
check("운임에 통행료 · 오지 할증 반영", f1["toll"] > 0 and f1["remote"] > 0, f"통행료 {f1['toll']:,} · 오지 {f1['remote']:,}")
e2 = pricing.estimate("25톤 무진동", "울산 남구", "경기 파주시", market=calm, max_item_mm=9500,
                      pickup_at=datetime.now() + timedelta(days=2))
f2 = {x["key"]: x["amount"] for x in e2["factors"]}
check("장재물 할증 · 조기 예약 할인 반영", f2["long"] > 0 and f2["reservation"] < 0)

section("파손주의 · 냉동 최상단 배치")
from backend.engines.packer.engine import Options  # noqa: E402
from backend.engines.packer.models import ItemSpec  # noqa: E402
from backend.engines.packer.service import plan  # noqa: E402
from backend.engines.packer.validate import validate  # noqa: E402
r = plan([ItemSpec("팔레트", 1100, 1100, 1000, 300, qty=4), ItemSpec("유리", 800, 600, 500, 40, qty=3, fragile=True),
          ItemSpec("냉동", 600, 400, 300, 15, qty=4, frozen=True)], truck_type="11톤 냉동탑차", cold_chain=True,
         options=Options(record_steps=False))
top = next(c for c in r["constraints"] if c["key"] == "top_only")
check("파손주의 · 냉동 상자 위 적재 없음", top["status"] == "pass" and not r["unplaced"], top["detail"])
rows = [dict(item_id="A", x=0, y=0, z=0, length=1000, width=1000, height=500, weight_kg=10, orig=[1000, 1000, 500],
             upright=True, stackable=True, max_top_load=None, drop=1, frozen=True),
        dict(item_id="B", x=0, y=0, z=500, length=1000, width=1000, height=500, weight_kg=10, orig=[1000, 1000, 500],
             upright=True, stackable=True, max_top_load=None, drop=1)]
chk = next(c for c in validate(TRUCKS["5톤 냉동탑차"], rows, min_support=0.8, lifo=False, cold_chain=True)
           if c["key"] == "top_only")
check("냉동 상자 위에 다른 상자 → 위반", chk["status"] == "fail")

finish()
