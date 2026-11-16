"""
매칭 2단계: LightGBM 기사 수락 예측 스코어링  ※ 가상(시뮬레이션) 데이터 기반

실제 운행 · 수락 기록이 아직 없어서, 가상 기사들의 '수락 행동 모델'로 만든 데이터로 학습한다.
화면에는 항상 "가상 데이터 기반"이라고 표시한다. 실데이터가 쌓이면 같은 피처로 다시 학습하면 된다.

피처 (화물 × 기사 한 쌍)
  pickup_km    기사 위치 → 상차지 도로거리
  util         그 기사 차량 기준 적재율 (부피 · 중량 중 큰 값)
  fare_gap     제시 운임이 AI 적정 운임보다 높은 비율 (+0.1 = 10% 높음)
  trip_km      상차지 → 하차지 도로거리
  cold, urgent 콜드체인 · 긴급 여부 (0/1)
  accept_rate  그 기사의 과거 제안 수락률 (기록이 없으면 0.7)
출력: 수락 확률 0~1

모델 파일: backend/data/ml_ranker.txt (+ ml_ranker.json 학습 정보). 없으면 서버 시작 때 자동 학습(1초 내외).
다시 학습:  python -m backend.engines.matcher.ml_ranker
"""

from __future__ import annotations

import json
import math
import os
import random
import threading

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data")
MODEL_FILE = os.path.join(DATA_DIR, "ml_ranker.txt")
META_FILE = os.path.join(DATA_DIR, "ml_ranker.json")
FEATURES = ["pickup_km", "util", "fare_gap", "trip_km", "cold", "urgent", "accept_rate"]
SEED = 2026
N_SAMPLES = 8000

_lock = threading.Lock()
_model = None
_meta: dict | None = None


# ------------------------------------------------------------------ 가상 데이터 (기사 수락 행동 모델)
def _true_accept_prob(f: dict) -> float:
    """가상 기사의 '실제' 수락 성향 (모델은 이 식을 모른 채 데이터만 보고 배운다).
    가까울수록 · 적재함을 잘 채울수록 · 운임이 적정가보다 높을수록 · 수락률 높은 기사일수록 수락.
    상차 거리는 30km 넘어가면 급격히 싫어하고, 너무 짧은 운행(30km 미만)은 덜 선호."""
    z = (1.4
         - 0.035 * f["pickup_km"] - 0.04 * max(0.0, f["pickup_km"] - 30)
         + 1.6 * f["util"]
         + 6.0 * max(-0.3, min(0.3, f["fare_gap"]))
         - (0.6 if f["trip_km"] < 30 else 0.0) + 0.002 * min(f["trip_km"], 300)
         + 0.35 * f["urgent"] - 0.25 * f["cold"]
         + 2.2 * (f["accept_rate"] - 0.7))
    return 1 / (1 + math.exp(-z))


def generate_synthetic(n: int = N_SAMPLES, seed: int = SEED) -> tuple[list[list[float]], list[int]]:
    """가상 제안 n건 → (피처 행렬, 수락 여부)."""
    rng = random.Random(seed)
    X, y = [], []
    for _ in range(n):
        f = {
            "pickup_km": min(150.0, rng.expovariate(1 / 28)),
            "util": min(1.0, max(0.05, rng.betavariate(3, 2))),
            "fare_gap": rng.gauss(0.0, 0.12),
            "trip_km": rng.uniform(15, 420),
            "cold": float(rng.random() < 0.25),
            "urgent": float(rng.random() < 0.2),
            "accept_rate": min(1.0, max(0.1, rng.gauss(0.7, 0.15))),
        }
        X.append([f[k] for k in FEATURES])
        y.append(1 if rng.random() < _true_accept_prob(f) else 0)
    return X, y


def _auc(y: list[int], p: list[float]) -> float:
    pairs = sorted(zip(p, y))
    pos = sum(y)
    neg = len(y) - pos
    rank_sum = sum(i + 1 for i, (_, label) in enumerate(pairs) if label == 1)
    return (rank_sum - pos * (pos + 1) / 2) / (pos * neg) if pos and neg else 0.5


# ------------------------------------------------------------------ 학습 · 로드
def train(save: bool = True) -> dict:
    import lightgbm as lgb
    import numpy as np
    X, y = generate_synthetic()
    cut = int(len(X) * 0.8)
    params = {"objective": "binary", "learning_rate": 0.06, "num_leaves": 15, "min_data_in_leaf": 40,
              "feature_fraction": 0.9, "verbose": -1, "seed": SEED, "deterministic": True,
              # 거리가 멀수록 · 운임이 낮을수록 수락 확률이 올라가지 않게 (단조 제약)
              "monotone_constraints": [-1, 1, 1, 0, 0, 0, 1]}
    ds = lgb.Dataset(np.array(X[:cut]), label=np.array(y[:cut]), feature_name=FEATURES)
    model = lgb.train(params, ds, num_boost_round=180)
    pred = model.predict(np.array(X[cut:])).tolist()
    meta = {"model": "LightGBM (binary)", "data": "가상 데이터 (기사 수락 행동 시뮬레이션)",
            "samples": len(X), "train": cut, "test": len(X) - cut, "accept_ratio": round(sum(y) / len(y), 3),
            "auc": round(_auc(y[cut:], pred), 3), "features": FEATURES,
            "importance": {k: int(v) for k, v in zip(FEATURES, model.feature_importance("gain"))}}
    if save:
        os.makedirs(DATA_DIR, exist_ok=True)
        # LightGBM C 라이브러리는 한글 경로(예: '수정본')를 못 여는 경우가 있어 파이썬으로 문자열을 직접 쓴다
        with open(MODEL_FILE, "w", encoding="utf-8") as f:
            f.write(model.model_to_string())
        with open(META_FILE, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    return {"model": model, "meta": meta}


def _ensure():
    """모델을 한 번만 불러온다. 파일이 없으면 학습해서 저장. LightGBM 이 없으면 규칙식으로 대신한다."""
    global _model, _meta
    if _meta is not None:
        return
    with _lock:
        if _meta is not None:
            return
        try:
            import lightgbm as lgb
            if os.path.exists(MODEL_FILE) and os.path.exists(META_FILE):
                with open(MODEL_FILE, encoding="utf-8") as f:  # 한글 경로 대응: 문자열로 읽어 로드
                    _model = lgb.Booster(model_str=f.read())
                with open(META_FILE, encoding="utf-8") as f:
                    _meta = json.load(f)
            else:
                r = train(save=True)
                _model, _meta = r["model"], r["meta"]
        except Exception as e:  # noqa: BLE001 — LightGBM 미설치(로컬 테스트 등) → 규칙식 대체
            _model = None
            _meta = {"model": "규칙식 대체 (LightGBM 사용 불가)", "data": "가상 데이터", "error": str(e)[:120],
                     "auc": None, "samples": 0}


def info() -> dict:
    _ensure()
    return dict(_meta)


def predict(rows: list[dict]) -> list[float]:
    """화물 × 기사 피처 dict 목록 → 수락 확률 목록."""
    if not rows:
        return []
    _ensure()
    if _model is None:
        return [round(_true_accept_prob(r), 4) for r in rows]
    import numpy as np
    X = np.array([[float(r[k]) for k in FEATURES] for r in rows])
    return [round(float(p), 4) for p in _model.predict(X)]


if __name__ == "__main__":
    r = train(save=True)
    print(json.dumps(r["meta"], ensure_ascii=False, indent=2))
