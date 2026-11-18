"""
Phase 5: Google Gemini 기반 능동형 AI 관제 (Function Calling)  — 키가 없으면 규칙 기반 에이전트로 대신

  모델  : settings.gemini_model (기본 gemini-3.5-flash-lite) · temperature 0.2 · flash 모델은 생각(thinking) 단계를 꺼서 빠르게
  도구  : 관제 데이터를 읽는 함수 + 매칭 설정을 바꾸는 함수 1개 (관리자가 명시적으로 요청했을 때만)
          → Gemini 가 필요한 함수를 골라 호출하고, 결과를 근거로 한국어로 답한다 (SDK 자동 함수 호출).
  안전  : 설정 변경은 함수 안에서 바로 적용하지 않고 '적용 대기'로 모았다가, 응답이 끝난 뒤 서버가 범위를
          검사해서 적용하고 감사 로그에 남긴다. 화물 상태 · 결제는 바꿀 수 없다.

GEMINI_API_KEY 가 없거나, 라이브러리(google-genai)가 없거나, 호출이 실패하면
engines/ai/rule_agent.py 의 handle_command / incident_report 결과를 그대로 돌려준다 (engine 필드로 구분).
"""

from __future__ import annotations

import json
import time

from . import rule_agent

SYSTEM = (
    "너는 B2B 화물 배차 플랫폼 '배차의 민족'의 관제 보조 AI다. 한국어로 짧고 정확하게 답한다. "
    "반드시 제공된 함수로 데이터를 조회한 뒤 숫자 근거를 들어 답하고, 추측하지 않는다. "
    "조치가 필요하면 '액션 아이템' 목록으로 정리한다. 매칭 설정 변경 함수는 관리자가 명시적으로 바꾸라고 할 때만 호출한다."
)


CONTROL_TOWER_SYSTEM = (
    "너는 B2B 화물 배차 플랫폼 '배차의 민족' 통합관제센터의 'AI 관제 실장'이다. 한국어로 짧고 정확하게 답한다. "
    "너는 조회 · 요약 전용이다: 반드시 제공된 조회 함수로 데이터를 확인한 뒤 숫자 근거를 들어 답하고, 추측하지 않는다. "
    "지역을 물으면 get_region_status 를 쓴다. 데이터에 없는 지역이면 없다고 말한다. "
    "설정 변경(임계값 · 반경 · 제한시간 · 가중치)이나 조치(재매칭 · 보조금 · 배정) 요청에는 직접 실행할 수 없다고 말하고, "
    "다음 위치를 안내한다: 매칭 설정은 '매칭 분석' 화면의 [자동 매칭 설정], 매칭 실패 조치는 '전국 관제 지도'의 [매칭 실패], "
    "LightGBM 임계값은 서버 설정(ML_THRESHOLD)이라 실행 중 변경 기능이 없다. "
    "개인 연락처는 답하지 않는다. 답은 핵심 3~6줄, 필요하면 '조치 제안' 목록을 덧붙인다. "
    "함수는 꼭 필요한 것만 최소로(보통 1~2개) 호출한다. 인사 · 잡담에는 함수를 호출하지 말고 한두 문장으로 답한다."
)
MAX_TOOL_CALLS = 3      # 한 질문에 데이터 조회 최대 횟수 (조회 1번 = 구글 요청 1번 더 → 무료 분당 한도 보호)
QUOTA_MSG = "무료 사용량(1분당 한도)을 잠시 넘었습니다. 약 1분 뒤에 다시 물어봐 주세요."
PHONE_RE = r"01[016789][-\s]?\d{3,4}[-\s]?\d{4}"


def _mask(text: str) -> str:
    """도구 결과에서 전화번호를 가린다 (외부 AI API 로 개인 연락처를 보내지 않게)."""
    import re
    return re.sub(PHONE_RE, "010-****-****", text)


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


def _generate(settings, prompt, tools, system: str = SYSTEM, max_calls: int | None = None) -> str:
    """prompt: 문자열 또는 대화 목록 [{'role': 'user'|'model', 'parts': [{'text': ...}]}, ...]."""
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=settings.gemini_api_key)

    afc = types.AutomaticFunctionCallingConfig(maximum_remote_calls=max_calls) if max_calls else None

    def run(think):
        return client.models.generate_content(
            model=settings.gemini_model, contents=prompt,
            config=types.GenerateContentConfig(temperature=0.2, system_instruction=system, tools=tools,
                                               thinking_config=think, automatic_function_calling=afc))

    # flash 모델은 답하기 전 '생각' 단계를 끈다 → 관제 질의 응답 3~6초 (켜 두면 수십 초까지 걸림, 2026-11 실측)
    think = types.ThinkingConfig(thinking_budget=0) if "flash" in settings.gemini_model else None
    try:
        resp = run(think)
    except Exception as e:  # noqa: BLE001
        code = getattr(e, "code", None)
        if code == 400 and think is not None:      # 이 모델이 생각 끄기를 거부 → 기본 설정으로 한 번 더
            resp = run(None)
        elif code in (500, 503):                   # 구글 서버 일시 과부하 → 1초 뒤 한 번 더
            time.sleep(1)
            resp = run(think)
        else:
            raise
    return (resp.text or "").strip()


def _fail_reason(e: Exception) -> str:
    """Gemini 오류 → 관제 화면에 보일 짧은 사유 (키 값은 넣지 않음)."""
    code, status = getattr(e, "code", None), getattr(e, "status", None)
    if code == 429 or "RESOURCE_EXHAUSTED" in str(e):
        return "429 무료 할당량 초과"
    if code == 404:
        return "404 모델 없음 → .env 의 GEMINI_MODEL 확인 (예: gemini-3.5-flash-lite)"
    if code in (400, 401, 403) and "API key" in str(e):
        return f"{code} API 키 오류 → .env 의 GEMINI_API_KEY 확인"
    return f"{code} {status}" if code else type(e).__name__


_QUOTA_BLOCK: dict[str, float] = {}   # 모델 → 하루 한도가 풀리는 시각(epoch). 그 전엔 Gemini 를 부르지 않음


def _quota_info(e: Exception) -> tuple[bool, float | None]:
    """429 오류 → (하루 한도인가, 다시 시도까지 초). 무료 티어 한도는 '분당'과 '하루' 두 가지."""
    import re
    text = str(e)
    per_day = "PerDay" in text
    m = re.search(r"retryDelay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)s", text)
    return per_day, float(m.group(1)) if m else None


def _hours(sec: float) -> str:
    return f"{int(sec // 3600)}시간 {int(sec % 3600 // 60)}분" if sec >= 3600 else f"{max(1, int(sec // 60))}분"


def _readonly_tools(store, used: list[str]):
    """AI 관제 실장용: 조회 함수만 (설정 변경 함수 없음) + 지역 상황. 결과는 전화번호를 가리고, 어떤 함수를 썼는지 기록."""
    import functools

    def get_region_status(region: str) -> str:
        """지역 이름(예: '강남구', '부산', '평택', '인천항')이 들어간 화물 상태 · 30분 이상 지연 · 미조치 이상 감지 · 권역 수급."""
        return json.dumps(rule_agent.region_status(store, region), ensure_ascii=False, default=str)

    base = [t for t in _tools(store, []) if t.__name__ != "update_matching_settings"] + [get_region_status]

    def wrap(fn):
        @functools.wraps(fn)
        def inner(*args, **kwargs):
            used.append(fn.__name__)
            for _ in range(2):          # 다른 요청이 데이터를 바꾸는 순간이면 한 번 더 (dict 크기 변경)
                try:
                    return _mask(fn(*args, **kwargs))
                except RuntimeError:
                    continue
            return "조회 실패 (잠시 후 다시)"
        return inner

    return [wrap(t) for t in base]


def control_tower(store, settings, command: str, history: list[dict] | None = None) -> dict:
    """AI 관제 실장 (POST /api/v1/ai/control-tower): 조회 · 요약 전용 자연어 질의.
    history: 이전 대화 [{'role': 'user'|'assistant', 'text': ...}] (최근 6개만). 설정은 절대 바꾸지 않는다.
    Gemini 키가 없거나 호출이 실패하면(할당량 초과 포함) 규칙 기반 답으로 대신한다."""
    if not available(settings):
        return {**rule_agent.answer_readonly(store, command), "tools": [], "fallback": "GEMINI_API_KEY 없음 → 규칙 기반"}
    blocked = _QUOTA_BLOCK.get(settings.gemini_model, 0) - time.time()
    if blocked > 0:                             # 하루 무료 한도 소진 → 초기화까지 Gemini 를 부르지 않고 규칙 기반으로
        return {**rule_agent.answer_readonly(store, command), "tools": [],
                "fallback": f"오늘 Gemini 무료 한도 소진 · 약 {_hours(blocked)} 뒤 초기화 → 규칙 기반", "quota": True}
    contents = [{"role": "model" if h.get("role") == "assistant" else "user", "parts": [{"text": str(h.get("text", ""))[:2000]}]}
                for h in (history or [])[-6:] if h.get("text")]
    contents.append({"role": "user", "parts": [{"text": command}]})
    used: list[str] = []
    try:
        answer = _generate(settings, contents, _readonly_tools(store, used), system=CONTROL_TOWER_SYSTEM,
                           max_calls=MAX_TOOL_CALLS)
    except Exception as e:  # noqa: BLE001
        reason = _fail_reason(e)
        if reason.startswith("429"):
            per_day, wait = _quota_info(e)
            if per_day:                         # 하루 한도: 기다리라고만 하지 말고 규칙 기반으로 바로 답 + 초기화까지 호출 멈춤
                _QUOTA_BLOCK[settings.gemini_model] = time.time() + (wait or 3600)
                return {**rule_agent.answer_readonly(store, command), "tools": used,
                        "fallback": f"오늘 Gemini 무료 한도 소진 · 약 {_hours(wait or 3600)} 뒤 초기화 → 규칙 기반", "quota": True}
            return {"engine": f"Gemini {settings.gemini_model}", "answer": QUOTA_MSG, "tools": used,   # 분당 한도
                    "fallback": "429 무료 할당량(분당) 초과", "quota": True}
        return {**rule_agent.answer_readonly(store, command), "tools": [],      # 키 오류 · 모델 없음 · 네트워크 → 규칙 기반
                "fallback": f"Gemini 호출 실패 ({reason}) → 규칙 기반"}
    if not answer:                              # 조회 횟수 한도에서 멈춰 답 문장이 없으면 규칙 기반으로
        return {**rule_agent.answer_readonly(store, command), "tools": used,
                "fallback": f"Gemini 답 없음 (조회 {len(used)}회 · 한도 {MAX_TOOL_CALLS}) → 규칙 기반"}
    return {"engine": f"Gemini {settings.gemini_model}", "answer": _mask(answer), "tools": used}


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
