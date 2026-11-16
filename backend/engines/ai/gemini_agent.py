"""
Phase 5: Google Gemini 기반 능동형 AI 관제 (Function Calling)  — 키가 없으면 규칙 기반 에이전트로 대신

  모델  : settings.gemini_model (기본 gemini-2.5-flash) · temperature 0.2
  도구  : 관제 데이터를 읽는 함수 + 매칭 설정을 바꾸는 함수 1개 (관리자가 명시적으로 요청했을 때만)
          → Gemini 가 필요한 함수를 골라 호출하고, 결과를 근거로 한국어로 답한다 (SDK 자동 함수 호출).
  안전  : 설정 변경은 함수 안에서 바로 적용하지 않고 '적용 대기'로 모았다가, 응답이 끝난 뒤 서버가 범위를
          검사해서 적용하고 감사 로그에 남긴다. 화물 상태 · 결제는 바꿀 수 없다.

GEMINI_API_KEY 가 없거나, 라이브러리(google-genai)가 없거나, 호출이 실패하면
engines/ai/rule_agent.py 의 handle_command / incident_report 결과를 그대로 돌려준다 (engine 필드로 구분).
"""

from __future__ import annotations

import json

from . import rule_agent

SYSTEM = (
    "너는 B2B 화물 배차 플랫폼 '배차의 민족'의 관제 보조 AI다. 한국어로 짧고 정확하게 답한다. "
    "반드시 제공된 함수로 데이터를 조회한 뒤 숫자 근거를 들어 답하고, 추측하지 않는다. "
    "조치가 필요하면 '액션 아이템' 목록으로 정리한다. 매칭 설정 변경 함수는 관리자가 명시적으로 바꾸라고 할 때만 호출한다."
)


def available(settings) -> bool:
    if not settings.gemini_api_key:
        return False
    try:
        import importlib.util
        return importlib.util.find_spec("google.genai") is not None
    except Exception:  # noqa: BLE001 — google 패키지 자체가 없음
        return False


def _tools(store, pending: list[dict]):
    """Gemini 에 넘길 함수들 (타입 힌트 · docstring 이 함수 설명으로 쓰인다)."""

    def get_control_summary() -> str:
        """관제 현황 요약: 운행 · 공차 · 매칭 대기 · 매칭 실패 · 이상 감지 · 지연 · 블랙리스트 · 서지 권역 수."""
        return json.dumps(store.admin_summary(), ensure_ascii=False)

    def get_failed_matches() -> str:
        """매칭 실패 화물 목록과 실패 원인 · 추천 운임."""
        rows = [{k: v[k] for k in ("id", "title", "origin", "destination", "fare", "recommended", "reasons")}
                for v in store.failed_matches()]
        return json.dumps(rows, ensure_ascii=False)

    def get_open_anomalies() -> str:
        """조치되지 않은 이상 감지(온도 · 정체 · 경로 이탈 · 지연 · 재위탁 의심) 목록."""
        rows = [{k: a[k] for k in ("id", "type_label", "severity", "text", "detail", "driver", "plate", "location")}
                for a in store.list_anomalies("ALL", "UNSOLVED")]
        return json.dumps(rows, ensure_ascii=False)

    def get_tracking_status() -> str:
        """운행별 경로 이탈 거리 · 연속 이탈 횟수 · 지연(분) · 재위탁 의심 핑 수."""
        return json.dumps(store.tracking_status(), ensure_ascii=False, default=str)

    def get_market_status() -> str:
        """권역별 수요/공급(D/S) 비율 · 서지 할증 · 모의 날씨."""
        rows = [{k: m[k] for k in ("region", "demand", "supply", "ds_ratio", "surge", "rush_hour")}
                | {"weather": m["weather"]["label"]} for m in store.market_board()]
        return json.dumps(rows, ensure_ascii=False)

    def get_matching_metrics() -> str:
        """매칭 성능: 배차 지연 p95 · 공차 거리 단축률 · 거절률 · 임계값 차단 수 · 현재 설정."""
        m = store.matching_metrics()
        return json.dumps({k: v for k, v in m.items() if k != "recent"} | {"settings": store.settings},
                          ensure_ascii=False)

    def get_tuning_guide() -> str:
        """거절률 · 리뷰 평점 · 수급 기반 LightGBM 임계값 · 매칭 가중치 조절 가이드."""
        return json.dumps(rule_agent.tuning_guide(store), ensure_ascii=False)

    def get_driver_reputation() -> str:
        """기사별 평점 · 리뷰 태그 · 블랙리스트(이용 제한) 상태."""
        rows = [{k: r[k] for k in ("id", "name", "avg", "count", "top_tags", "blacklisted")}
                for r in store.reputation_board()]
        return json.dumps(rows, ensure_ascii=False)

    def update_matching_settings(radius_km: int, timeout_sec: int, w_dist: float) -> str:
        """자동 매칭 설정 변경 요청 (탐색 반경 10~200km · 수락 제한시간 15~300초 · 거리 가중치 0~1).
        관리자가 명시적으로 바꾸라고 했을 때만 호출한다. 서버가 범위를 확인한 뒤 적용한다."""
        pending.append({"radius_km": int(radius_km), "timeout_sec": int(timeout_sec), "w_dist": float(w_dist)})
        return "적용 대기열에 등록됨 (응답 후 서버가 검증 · 적용)"

    return [get_control_summary, get_failed_matches, get_open_anomalies, get_tracking_status, get_market_status,
            get_matching_metrics, get_tuning_guide, get_driver_reputation, update_matching_settings]


def _generate(settings, prompt: str, tools) -> str:
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=settings.gemini_api_key)
    resp = client.models.generate_content(
        model=settings.gemini_model, contents=prompt,
        config=types.GenerateContentConfig(temperature=0.2, system_instruction=SYSTEM, tools=tools))
    return (resp.text or "").strip()


def ask(store, settings, text: str) -> dict:
    """관제 자연어 명령. Gemini 응답 텍스트 + (있으면) 적용된 설정 변경. 실패하면 규칙 기반 결과."""
    if not available(settings):
        return {**rule_agent.handle_command(store, text), "fallback": "GEMINI_API_KEY 없음 → 규칙 기반"}
    pending: list[dict] = []
    try:
        answer = _generate(settings, text, _tools(store, pending))
    except Exception as e:  # noqa: BLE001 — 키 오류 · 네트워크 · 할당량 → 규칙 기반
        return {**rule_agent.handle_command(store, text), "fallback": f"Gemini 호출 실패 ({type(e).__name__})"}
    return {"engine": f"Gemini {settings.gemini_model}", "answer": answer or "(빈 응답)", "pending": pending}


def apply_pending(store, pending: list[dict]) -> list[dict]:
    """Gemini 가 요청한 설정 변경을 범위 검사 후 적용 (이벤트 루프에서 호출)."""
    applied = []
    for p in pending:
        if 10 <= p["radius_km"] <= 200 and 15 <= p["timeout_sec"] <= 300 and 0 <= p["w_dist"] <= 1:
            store.update_settings(p, by="AI 관제 (Gemini)")
            applied.append(p)
    return applied


def incident_report(store, settings, anomaly_id: str) -> dict:
    base = rule_agent.incident_report(store, anomaly_id)
    if not base["ok"] or not available(settings):
        return {**base, "fallback": None if available(settings) else "GEMINI_API_KEY 없음 → 규칙 기반"}
    prompt = ("다음 이상 감지 건에 대한 관제 인시던트 리포트를 작성해라. 형식: 상황 요약 2줄, 추정 원인, "
              "액션 아이템(번호 목록 3~5개), 화주 안내 문구 1개. 근거 데이터:\n" + base["text"])
    try:
        text = _generate(settings, prompt, _tools(store, []))
    except Exception as e:  # noqa: BLE001
        return {**base, "fallback": f"Gemini 호출 실패 ({type(e).__name__})"}
    return {**base, "engine": f"Gemini {settings.gemini_model}", "text": text or base["text"]}
