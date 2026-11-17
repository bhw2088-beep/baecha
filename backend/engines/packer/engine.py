"""
규칙(Rule) + Extreme Point(EP) 기반 3D 적재 엔진.

참고: Crainic, Perboli, Tadei (2008) "Extreme Point-Based Heuristics for Three-Dimensional Bin Packing"
적재 규칙 R0~R10 의 정의 · 이름 · 미적재 사유는 rules.py 한 곳에 있다 (검증기 · 화면과 공용).

알고리즘 개요
  1. 정렬 규칙: 하차 순서(늦게 내릴 것 먼저) → 적층 가능 품목 먼저(파손주의 · 냉동은 나중=위쪽)
               → 선택한 기준(면적-높이 군집 / 부피 / 무게) 내림차순
  2. 상자마다 현재 EP 목록을 배치 우선순위(merit)로 정렬하고,
     첫 번째로 '모든 배치 규칙을 만족하는' EP × 방향 조합을 찾는다 (first-fit over sorted EPs).
       - wall  : (x, z, y)  운전석 쪽부터 벽을 쌓듯이 → 다중 하차/LIFO 에 유리
       - floor : (z, x, y)  바닥부터 채움 → 무게중심이 낮음
  3. R10 좌우 균형: 찾은 자리와 같은 줄(같은 x · z)의 반대편(오른쪽 벽 또는 오른쪽 상자 옆)에도 놓아 보고,
     좌우 무게중심이 차체 가운데에 더 가까워지는 쪽을 고른다. (EP 는 왼쪽 벽부터 채워서 그대로 두면 한쪽으로 쏠림)
  4. 배치한 상자의 세 모서리 (x+dx,y,z) (x,y+dy,z) (x,y,z+dz) 를 다른 축 방향으로
     '투영'해서 새 EP 를 만든다 (가장 가까운 상자 면 또는 벽/바닥에 닿을 때까지).
     → 단순 모서리(pivot)만 쓰는 방식보다 빈틈을 훨씬 잘 찾는다.

배치 중 차단 규칙 — 하나라도 어기면 그 위치에는 놓지 않는다
  R1 적재함 경계   R2 상자 간 비중첩   R3 방향(세움 고정)   R4 최대 적재중량
  R5 지지면 비율(공중 부양 금지)   R6 적층 불가/상부 허용하중(하중을 아래로 전파해서 검사)
  R7 파손주의 · 냉동 최상단(위에 적재 금지)
  R8 하차 순서(LIFO): 먼저 내릴 화물 위/앞(도어 쪽)을 나중에 내릴 화물이 막으면 안 됨
     (측면 개방 차량은 '위에 얹힘'만 검사)
  R9 보수적 안전율: 부피 · 중량을 (1 − 안전율)까지만 싣는다
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass

import numpy as np

from .models import Box, Placement
from .rules import unplaced_reason
from .trucks import Container

EPS = 1e-6


@dataclass
class Options:
    merit: str = "wall"            # wall | floor
    sort: str = "area_height"      # area_height | volume | weight
    min_support: float = 0.8       # R5 바닥 면적 대비 지지 비율
    lifo: bool = True              # R8
    safety_margin: float = 0.0     # R9 (서비스가 config.safety_margin 을 넣어 줌 · 0 = 한도 없음)
    balance: bool = True           # R10 좌우 균형 자리 선택
    record_steps: bool = True
    max_step_snapshots: int = 400


class EPPacker:
    def __init__(self, container: Container, options: Options | None = None):
        self.c = container
        self.o = options or Options()

    # ------------------------------------------------------------------ sort
    def sort_boxes(self, boxes: list[Box]) -> list[Box]:
        floor_area = self.c.length * self.c.width

        def criterion(b: Box):
            if self.o.sort == "volume":
                return (-b.volume, -b.h)
            if self.o.sort == "weight":
                return (-b.weight, -b.volume)
            # Clustered Area-Height: 바닥면적을 적재함 바닥의 2% 단위로 묶은 뒤 높이 내림차순
            cluster = int((b.l * b.w) * 100 / floor_area // 2)
            return (-cluster, -b.h, -b.weight)

        return sorted(boxes, key=lambda b: (-b.drop, 0 if b.stackable else 1, *criterion(b)))

    # ------------------------------------------------------------------ main
    def pack(self, boxes: list[Box]) -> dict:
        t0 = time.perf_counter()
        order = self.sort_boxes(boxes)
        n = len(order)
        # 배치된 상자들의 좌표 배열 (numpy 로 벡터 연산)
        self.X0, self.X1 = np.zeros(n), np.zeros(n)
        self.Y0, self.Y1 = np.zeros(n), np.zeros(n)
        self.Z0, self.Z1 = np.zeros(n), np.zeros(n)
        self.DROP = np.zeros(n)
        self.k = 0
        self.placed: list[Placement] = []
        self.sup: list[list[tuple[int, float]]] = []  # 상자별 (아래 지지 상자 index, 접촉 면적)
        self.total_weight = 0.0
        self.total_volume = 0
        self.weight_y = 0.0  # Σ 무게 × 좌우 중심 (R10 좌우 무게중심 계산용)
        # R9 보수적 안전율: 부피 · 중량을 (1 − 안전율)까지만
        cap = 1 - max(0.0, self.o.safety_margin)
        self.weight_limit = self.c.max_weight * cap
        self.volume_limit = self.c.volume * cap
        eps: list[tuple[int, int, int]] = [(0, 0, 0)]
        snapshots: list[list[list[int]]] = []
        unplaced = []

        def skip(box: Box, fail_key: str):
            unplaced.append({"item_id": box.uid, "name": box.name, "sku": box.sku, "reason": unplaced_reason(fail_key)})

        for box in order:
            if self.total_weight + box.weight > self.c.max_weight + EPS:          # R4
                skip(box, "weight")
                continue
            if (self.total_weight + box.weight > self.weight_limit + EPS           # R9
                    or self.total_volume + box.volume > self.volume_limit + EPS):
                skip(box, "safety")
                continue
            eps.sort(key=self._merit)
            fails: Counter = Counter()
            choice = self._first_fit(box, eps, fails)
            if choice is None:
                skip(box, next((r for r, _ in fails.most_common() if r != "space"), "space") if fails else "space")
                continue
            if self.o.record_steps and len(snapshots) < self.o.max_step_snapshots:
                snapshots.append([list(p) for p in eps])
            p = choice
            p.step = len(self.placed) + 1
            self._commit(p)
            eps = self._update_eps(eps, p)

        return {
            "placements": self.placed,
            "unplaced": unplaced,
            "steps": snapshots,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
            "sorted_ids": [b.uid for b in order],
        }

    def _merit(self, p):
        x, y, z = p
        return (x, z, y) if self.o.merit == "wall" else (z, x, y)

    # ------------------------------------------------------------------ placement search
    def _first_fit(self, box: Box, eps, fails: Counter) -> Placement | None:
        for ep in eps:
            best = None
            for dx, dy, dz in box.orientations():
                cand = self._try(box, ep, dx, dy, dz, fails)
                if cand is None:
                    continue
                # 같은 EP 에서는 윗면이 낮고(안정), 바닥이 넓은 방향을 고른다
                key = (cand.z1, -(dx * dy), dx)
                if best is None or key < best[0]:
                    best = (key, cand)
            if best:
                cand = best[1]
                if self.o.balance:  # R10 좌우 균형: 같은 줄 반대편 자리가 더 균형이 맞으면 그쪽
                    alt = self._mirror(box, cand)
                    if alt is not None and self._lateral_after(alt) + EPS < self._lateral_after(cand):
                        alt.balanced = True
                        return alt
                return cand
        return None

    def _mirror(self, box: Box, p: Placement) -> Placement | None:
        """p 와 같은 x · z · 방향으로, +y 쪽에서 처음 닿는 면(오른쪽 벽 또는 상자)에 붙인 자리."""
        x, y, z, dx, dy, dz = p.x, p.y, p.z, p.dx, p.dy, p.dz
        k = self.k
        X0, X1, Y0, Z0, Z1 = (a[:k] for a in (self.X0, self.X1, self.Y0, self.Z0, self.Z1))
        m = (X0 < x + dx) & (X1 > x) & (Z0 < z + dz) & (Z1 > z) & (Y0 >= y + dy - EPS)
        wall = float(Y0[m].min()) if k and m.any() else float(self.c.width)
        y_r = int(wall) - dy
        if y_r <= y:
            return None
        return self._try(box, (x, y_r, z), dx, dy, dz, Counter())

    def _lateral_after(self, p: Placement) -> float:
        """p 를 놓았을 때 좌우 무게중심이 차체 가운데에서 벗어난 거리 (mm)."""
        w = self.total_weight + p.box.weight
        if w <= 0:
            return 0.0
        cy = (self.weight_y + p.box.weight * (p.y + p.dy / 2)) / w
        return abs(cy - self.c.width / 2)

    def _try(self, box: Box, ep, dx, dy, dz, fails: Counter) -> Placement | None:
        x, y, z = ep
        c = self.c
        # R1 경계  (R3 방향은 box.orientations() 가 세움 고정이면 높이축 유지 방향만 내준다)
        if x + dx > c.length or y + dy > c.width or z + dz > c.height:
            fails["space"] += 1
            return None
        k = self.k
        X0, X1, Y0, Y1, Z0, Z1 = (a[:k] for a in (self.X0, self.X1, self.Y0, self.Y1, self.Z0, self.Z1))
        # R2 비중첩
        if k and np.any((X0 < x + dx) & (X1 > x) & (Y0 < y + dy) & (Y1 > y) & (Z0 < z + dz) & (Z1 > z)):
            fails["space"] += 1
            return None
        # R5 지지면
        sup: list[tuple[int, float]] = []
        ratio = 1.0
        if z > 0:
            m = (np.abs(Z1 - z) < EPS) & (X0 < x + dx) & (X1 > x) & (Y0 < y + dy) & (Y1 > y)
            idx = np.nonzero(m)[0]
            if len(idx) == 0:
                fails["support"] += 1
                return None
            areas = (np.minimum(X1[idx], x + dx) - np.maximum(X0[idx], x)) *                     (np.minimum(Y1[idx], y + dy) - np.maximum(Y0[idx], y))
            ratio = float(areas.sum() / (dx * dy))
            if ratio + EPS < self.o.min_support:
                fails["support"] += 1
                return None
            # R7 아래가 파손주의 · 냉동(최상단 전용) / R6 아래가 적층 불가 품목이면 불가
            below = [self.placed[i].box for i in idx]
            if any(b.fragile or b.frozen for b in below):
                fails["top"] += 1
                return None
            if any(not b.stackable for b in below):
                fails["nostack"] += 1
                return None
            sup = [(int(i), float(a)) for i, a in zip(idx, areas)]
        # 이 자리에 놓으면 '위에 이미 있는 상자'의 바닥을 새로 받쳐주게 되는 경우 (돌출부 아래 빈칸 채우기)
        above: list[tuple[int, float]] = []
        if k:
            m = (np.abs(Z0 - (z + dz)) < EPS) & (X0 < x + dx) & (X1 > x) & (Y0 < y + dy) & (Y1 > y)
            ia = np.nonzero(m)[0]
            if len(ia):
                if not box.stackable:
                    fails["top" if box.fragile or box.frozen else "nostack"] += 1
                    return None
                areas = (np.minimum(X1[ia], x + dx) - np.maximum(X0[ia], x)) *                         (np.minimum(Y1[ia], y + dy) - np.maximum(Y0[ia], y))
                above = [(int(i), float(a)) for i, a in zip(ia, areas)]
        # R6 상부 허용하중: 후보를 포함한 전체 하중 분포를 다시 계산해서 모든 상자의 허용하중 확인
        loads = self._loads(box, z, sup, above)
        if loads is None:
            fails["load"] += 1
            return None
        # R8 하차 순서
        if self.o.lifo and k and self._blocks(box.drop, x, y, z, dx, dy, dz):
            fails["lifo"] += 1
            return None
        p = Placement(box=box, x=x, y=y, z=z, dx=dx, dy=dy, dz=dz, ep=(x, y, z), support_ratio=ratio)
        p._pending = (sup, above, loads)  # 확정(commit) 때 재사용
        return p

    def _loads(self, box: Box, z: int, sup, above) -> list[float] | None:
        """후보 상자를 넣었다고 가정하고, 위에서 아래로 하중(자기 무게 + 위에서 받은 하중)을
        접촉 면적 비율대로 아래 상자들에 나눠 준다. 허용하중을 넘는 상자가 있으면 None."""
        k = self.k
        n = k + 1
        extra = dict(above)  # 위 상자 a 의 지지 목록에 후보(index k)가 추가됨
        z0 = [p.z for p in self.placed] + [z]
        weight = [p.box.weight for p in self.placed] + [box.weight]
        load = [0.0] * n
        for i in sorted(range(n), key=lambda i: -z0[i]):
            lst = sup if i == k else (self.sup[i] + [(k, extra[i])] if i in extra else self.sup[i])
            tot = sum(a for _, a in lst)
            if tot <= 0:
                continue
            f = (weight[i] + load[i]) / tot
            for j, a in lst:
                load[j] += f * a
        caps = [p.box.cap for p in self.placed] + [box.cap]
        if any(load[i] > caps[i] + EPS for i in range(n)):
            return None
        return load

    def _blocks(self, drop, x, y, z, dx, dy, dz) -> bool:
        """새 상자(drop)와 기존 상자들 사이에 '먼저 내릴 화물을 막는' 관계가 생기는지."""
        k = self.k
        X0, X1, Y0, Y1, Z0, Z1, D = (a[:k] for a in (self.X0, self.X1, self.Y0, self.Y1, self.Z0, self.Z1, self.DROP))
        xy = (X0 < x + dx) & (X1 > x) & (Y0 < y + dy) & (Y1 > y)
        yz = (Y0 < y + dy) & (Y1 > y) & (Z0 < z + dz) & (Z1 > z)
        earlier = D < drop   # 기존 상자가 먼저 내림 → 새 상자가 그 위/앞에 있으면 안 됨
        later = D > drop     # 새 상자가 먼저 내림 → 기존 상자가 새 상자 위/앞에 있으면 안 됨
        bad = earlier & xy & (z >= Z1 - EPS)
        bad |= later & xy & (Z0 >= z + dz - EPS)
        if not self.c.side_access:
            bad |= earlier & yz & (x >= X1 - EPS)
            bad |= later & yz & (X0 >= x + dx - EPS)
        return bool(np.any(bad))

    def _commit(self, p: Placement):
        i = self.k
        self.X0[i], self.X1[i] = p.x, p.x1
        self.Y0[i], self.Y1[i] = p.y, p.y1
        self.Z0[i], self.Z1[i] = p.z, p.z1
        self.DROP[i] = p.box.drop
        self.k += 1
        self.placed.append(p)
        self.total_weight += p.box.weight
        self.total_volume += p.dx * p.dy * p.dz
        self.weight_y += p.box.weight * (p.y + p.dy / 2)
        sup, above, loads = p._pending
        del p._pending
        self.sup.append(sup)
        for a, area in above:  # 위 상자의 지지 목록·지지 비율 갱신
            self.sup[a].append((i, area))
            q = self.placed[a]
            q.support_ratio = min(1.0, sum(ar for _, ar in self.sup[a]) / (q.dx * q.dy))
        for j, q in enumerate(self.placed):
            q.load = loads[j]
            tot = sum(ar for _, ar in self.sup[j])
            q.supporters = [(s, ar / tot) for s, ar in self.sup[j]] if tot else []

    # ------------------------------------------------------------------ extreme points
    def _proj(self, axis: str, px, py, pz) -> int:
        """점을 axis 의 음의 방향으로 밀어서 처음 닿는 면(상자 또는 벽/바닥)의 좌표."""
        k = self.k
        X0, X1, Y0, Y1, Z0, Z1 = (a[:k] for a in (self.X0, self.X1, self.Y0, self.Y1, self.Z0, self.Z1))
        if axis == "z":
            m = (X0 <= px) & (px < X1) & (Y0 <= py) & (py < Y1) & (Z1 <= pz + EPS)
            return int(Z1[m].max()) if m.any() else 0
        if axis == "y":
            m = (X0 <= px) & (px < X1) & (Z0 <= pz) & (pz < Z1) & (Y1 <= py + EPS)
            return int(Y1[m].max()) if m.any() else 0
        m = (Y0 <= py) & (py < Y1) & (Z0 <= pz) & (pz < Z1) & (X1 <= px + EPS)
        return int(X1[m].max()) if m.any() else 0

    def _occupied(self, px, py, pz) -> bool:
        k = self.k
        return bool(np.any((self.X0[:k] <= px) & (px < self.X1[:k]) & (self.Y0[:k] <= py) & (py < self.Y1[:k])
                           & (self.Z0[:k] <= pz) & (pz < self.Z1[:k])))

    def _update_eps(self, eps, p: Placement):
        x, y, z, x1, y1, z1 = p.x, p.y, p.z, p.x1, p.y1, p.z1
        new = [
            (x1, y, z), (x1, self._proj("y", x1, y, z), z), (x1, y, self._proj("z", x1, y, z)),
            (x, y1, z), (self._proj("x", x, y1, z), y1, z), (x, y1, self._proj("z", x, y1, z)),
            (x, y, z1), (self._proj("x", x, y, z1), y, z1), (x, self._proj("y", x, y, z1), z1),
        ]
        c = self.c
        merged = set()
        for pt in [*eps, *new]:
            px, py, pz = pt
            if px >= c.length or py >= c.width or pz >= c.height:
                continue
            if self._occupied(px, py, pz):
                continue
            merged.add((int(px), int(py), int(pz)))
        return list(merged)


def summarize(container: Container, placements: list[Placement] | list[dict], total_boxes: int,
              total_weight_all: float) -> dict:
    rows = [p.to_dict() if isinstance(p, Placement) else p for p in placements]
    vol = sum(r["length"] * r["width"] * r["height"] for r in rows)
    wt = sum(r["weight_kg"] for r in rows)
    if wt > 0:
        cx = sum((r["x"] + r["length"] / 2) * r["weight_kg"] for r in rows) / wt
        cy = sum((r["y"] + r["width"] / 2) * r["weight_kg"] for r in rows) / wt
        cz = sum((r["z"] + r["height"] / 2) * r["weight_kg"] for r in rows) / wt
    else:
        cx = cy = cz = 0.0
    return {
        "placed": len(rows), "total": total_boxes, "unplaced": total_boxes - len(rows),
        "volume_util": round(vol / container.volume, 4),
        "weight_util": round(wt / container.max_weight, 4) if container.max_weight else 0,
        "loaded_weight": round(wt, 1), "requested_weight": round(total_weight_all, 1),
        "loaded_volume_m3": round(vol / 1e9, 2),
        "max_height": max((r["z"] + r["height"] for r in rows), default=0),
        "used_length": max((r["x"] + r["length"] for r in rows), default=0),
        "cog": {"x": round(cx), "y": round(cy), "z": round(cz),
                "x_ratio": round(cx / container.length, 3) if rows else None,
                "y_offset_ratio": round(abs(cy - container.width / 2) / container.width, 3) if rows else None},
    }

