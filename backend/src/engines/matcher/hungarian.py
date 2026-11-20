"""
매칭 3단계: 헝가리안 알고리즘 최적 배차 (여러 화물 × 여러 기사를 한 번에)

1순위 기사를 화물마다 따로 고르면(탐욕 방식) 한 기사가 여러 화물의 1순위가 될 때
먼저 처리된 화물이 그 기사를 가져가 전체 품질이 떨어진다.
헝가리안 알고리즘은 '모든 화물 · 기사 쌍의 점수 합'이 최대가 되도록 1:1 배정을 한 번에 구한다.

입력: 화물 id 목록, 기사 id 목록, 점수 {(화물, 기사): 0~1} (후보가 아닌 쌍은 없음)
출력: [(화물, 기사, 점수)] — 후보 쌍으로만 이루어진 최적 배정
"""

from __future__ import annotations

INFEASIBLE = 1e6  # 후보가 아닌 쌍의 비용 (배정되면 버린다)


def assign(order_ids: list[str], driver_ids: list[str],
           score: dict[tuple[str, str], float]) -> list[tuple[str, str, float]]:
    if not order_ids or not driver_ids or not score:
        return []
    import numpy as np
    from scipy.optimize import linear_sum_assignment

    cost = np.full((len(order_ids), len(driver_ids)), INFEASIBLE)
    for i, o in enumerate(order_ids):
        for j, d in enumerate(driver_ids):
            s = score.get((o, d))
            if s is not None:
                cost[i, j] = 1.0 - s  # 점수 최대화 = 비용(1 − 점수) 최소화
    rows, cols = linear_sum_assignment(cost)  # 정사각이 아니어도 됨 (작은 쪽 개수만큼 배정)
    out = []
    for i, j in zip(rows, cols):
        if cost[i, j] < INFEASIBLE:
            out.append((order_ids[i], driver_ids[j], score[(order_ids[i], driver_ids[j])]))
    return out


def greedy(order_ids: list[str], driver_ids: list[str],
           score: dict[tuple[str, str], float]) -> list[tuple[str, str, float]]:
    """비교용: 화물 순서대로 남은 기사 중 최고점 기사를 고르는 탐욕 방식 (이전 방식)."""
    used, out = set(), []
    for o in order_ids:
        best = max(((d, score[(o, d)]) for d in driver_ids if (o, d) in score and d not in used),
                   key=lambda x: x[1], default=None)
        if best:
            used.add(best[0])
            out.append((o, best[0], best[1]))
    return out


def nearest(order_ids: list[str], driver_ids: list[str],
            km: dict[tuple[str, str], float]) -> list[tuple[str, str, float]]:
    """KPI 기준선(Baseline): '단순 최단거리 매칭' — 등록 순서대로, 남은 후보 중 상차지까지 가장 가까운 공차에게.
    점수 · AI 예측 없이 거리만 보는 기존 콜 배차 방식. 반환 (화물, 기사, 상차 거리 km)."""
    used, out = set(), []
    for o in order_ids:
        best = min(((d, km[(o, d)]) for d in driver_ids if (o, d) in km and d not in used),
                   key=lambda x: x[1], default=None)
        if best:
            used.add(best[0])
            out.append((o, best[0], best[1]))
    return out
