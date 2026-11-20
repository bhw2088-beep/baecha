"""KPI 판정 (engines/kpi) 단위 테스트."""

import pytest

from src.engines import kpi


def test_ctor_kpi_table():
    assert len(kpi.KPIS) == len(kpi.BY_KEY) == 8
    assert {"dispatch_p95_sec", "rejection_rate_pct", "packing_violation_pct", "gps_false_alarm_pct"} <= kpi.BY_KEY.keys()


def test_evaluate_smoke():
    r = kpi.evaluate("dispatch_p95_sec", 0.4, source="테스트")
    assert {"key", "name", "value", "display", "status", "status_label", "source"} <= r.keys()
    assert r["display"] == "0.40초"


@pytest.mark.parametrize("key, value, status", [
    ("dispatch_p95_sec", 2.99, "pass"), ("dispatch_p95_sec", 3.0, "fail"),
    ("rejection_rate_pct", 14.9, "pass"), ("rejection_rate_pct", 17.0, "warn"), ("rejection_rate_pct", 21.0, "fail"),
    ("deadhead_vs_nearest_pct", 12.0, "pass"), ("deadhead_vs_nearest_pct", 5.0, "warn"),
    ("deadhead_vs_nearest_pct", -1.0, "fail"),
    ("packing_violation_pct", 0.0, "pass"), ("packing_violation_pct", 0.1, "fail"),
    ("pg_success_pct", 99.5, "pass"), ("pg_success_pct", 98.0, "fail"),
    ("dispatch_p95_sec", None, "none"),
])
def test_evaluate_actual_thresholds(key, value, status):
    assert kpi.evaluate(key, value, source="테스트")["status"] == status


def test_summary_actual_counts_measured_only():
    rows = [kpi.evaluate("dispatch_p95_sec", 1.0, source="t"), kpi.evaluate("rejection_rate_pct", 17.0, source="t"),
            kpi.evaluate("pg_success_pct", 90.0, source="t"), kpi.evaluate("relay_p95_sec", None, source="t")]
    assert kpi.summary(rows) == {"measured": 3, "total": 4, "pass": 1, "warn": 1, "fail": 1}
