"""
Phase 0: 가입 컴플라이언스 엔진 (순수 함수 · 외부 의존성 없음 → pytest 로 바로 검증)

[화주 · 차주 사업자등록번호]
  1) 형식: 숫자 10자리 (000-00-00000)
  2) Modulus-10 검증 숫자: 앞 9자리 × 가중치(1 3 7 1 3 7 1 3 5) 합 + ⌊9번째 자리 × 5 ÷ 10⌋
     → (10 − 합 % 10) % 10 이 10번째 자리와 같아야 한다.   (mock_validate_business_number)
  3) 사업 상태: 국세청 API 키(NTS_API_KEY)가 있으면 실제 조회, 없으면 모의 상태 목록으로 판정
       계속사업자(01) · 휴업자(02) · 폐업자(03) · 미등록
     ※ Mod-10 은 '번호 형식'만 본다. 휴업 · 폐업 여부는 수학 공식으로 알 수 없어서 모의 목록을 따로 둔다.

[차량 번호판]  plate_type
  사업용 화물차는 노란색 번호판 + 용도 문자 '바 · 사 · 아 · 자'(택배 '배').
  → COMMERCIAL_YELLOW 만 배차 가능. 자가용(흰색) · 렌터카(허 · 하 · 호)는 매칭 후보에서 제외.

[톤수]  표준 화물차 톤수 (1 · 2.5 · 5 · 11 · 25톤) — 매칭 1단계 '기사 톤수 ≥ 화물 요구 톤수' 필터에 쓴다.
"""

from __future__ import annotations

import re
from datetime import datetime

# ------------------------------------------------------------------ 사업자등록번호
WEIGHTS = (1, 3, 7, 1, 3, 7, 1, 3, 5)

STATUS_LABEL = {
    "CONTINUING": "계속사업자",
    "SUSPENDED": "휴업자",
    "CLOSED": "폐업자",
    "UNREGISTERED": "국세청 미등록",
    "MISMATCH": "등록 정보 불일치",
    "UNAVAILABLE": "국세청 확인 불가 (잠시 후 다시 시도)",
    "CHECKSUM_FAIL": "검증 숫자 오류 (Mod-10)",
    "INVALID_FORMAT": "형식 오류 (숫자 10자리 아님)",
}

# 모의 상태 목록: Mod-10 은 통과하지만 휴업 · 폐업인 번호 (실패 시연용 · 국세청 키가 없을 때만 사용)
MOCK_SUSPENDED = {"6178140003"}
MOCK_CLOSED = {"6178150006", "1348600011"}

# 시연 · 테스트용 번호 셋 (모두 가상 번호 · 실제 기업과 무관)
SAMPLE_VALID = ["6178110004", "6178120007", "6178130000", "3058812340", "6218700017"]
SAMPLE_INVALID = {
    "1234567890": "CHECKSUM_FAIL",   # 흔한 가짜 번호
    "6178120003": "CHECKSUM_FAIL",   # 마지막 자리 하나 틀림
    "12345": "INVALID_FORMAT",
    "6178140003": "SUSPENDED",
    "6178150006": "CLOSED",
}


def normalize_biz_no(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def format_biz_no(value: str) -> str:
    d = normalize_biz_no(value)
    return f"{d[:3]}-{d[3:5]}-{d[5:]}" if len(d) == 10 else (value or "")


def check_digit(first9: str) -> int:
    d = [int(c) for c in first9]
    s = sum(x * w for x, w in zip(d, WEIGHTS)) + (d[8] * 5) // 10
    return (10 - s % 10) % 10


def mock_validate_business_number(value: str | None) -> bool:
    """Modulus-10 검증. 숫자 10자리이고 검증 숫자가 맞으면 True."""
    d = normalize_biz_no(value)
    return len(d) == 10 and check_digit(d[:9]) == int(d[9])


def with_check_digit(first9: str) -> str:
    """앞 9자리로 Mod-10 을 통과하는 10자리 번호를 만든다 (테스트 · 시드 데이터 생성용)."""
    return first9 + str(check_digit(first9))


def _result(code: str, source: str, biz_no: str, note: str = "", **extra) -> dict:
    return {"ok": code == "CONTINUING", "status": code, "label": STATUS_LABEL[code], "source": source,
            "source_label": "국세청 API" if source == "nts" else "모의 판정 (국세청 연동 전)",
            "biz_no": format_biz_no(biz_no), "note": note,
            "checked_at": datetime.now().isoformat(timespec="seconds"), **extra}


def mock_business_status(value: str | None) -> dict:
    """국세청 연동 전 모의 판정: 형식 → Mod-10 → 모의 휴업 · 폐업 목록 → 계속사업자."""
    d = normalize_biz_no(value)
    if len(d) != 10:
        return _result("INVALID_FORMAT", "mock", d)
    if not mock_validate_business_number(d):
        return _result("CHECKSUM_FAIL", "mock", d, note=f"검증 숫자는 {check_digit(d[:9])} 이어야 합니다")
    if d in MOCK_SUSPENDED:
        return _result("SUSPENDED", "mock", d)
    if d in MOCK_CLOSED:
        return _result("CLOSED", "mock", d)
    return _result("CONTINUING", "mock", d)


# ------------------------------------------------------------------ 국세청 API (선택)
NTS_BASE = "https://api.odcloud.kr/api/nts-businessman/v1"
NTS_CODE = {"01": "CONTINUING", "02": "SUSPENDED", "03": "CLOSED"}


def nts_business_status(value: str, api_key: str, owner: str | None = None, start_date: str | None = None,
                        timeout: float = 5.0) -> dict:
    """국세청_사업자등록정보 진위확인 및 상태조회 서비스 (공공데이터포털).
    대표자명 + 개업일자가 있으면 진위확인(/validate), 없으면 상태조회(/status).
    네트워크 오류 · 키 오류면 모의 판정으로 대신하고 note 에 사유를 남긴다."""
    d = normalize_biz_no(value)
    first = mock_business_status(d)
    if first["status"] in ("INVALID_FORMAT", "CHECKSUM_FAIL"):
        return first  # 형식이 틀린 번호는 API 를 부를 필요가 없다
    import requests
    try:
        if owner and start_date:
            body = {"businesses": [{"b_no": d, "start_dt": re.sub(r"\D", "", start_date), "p_nm": owner.strip()}]}
            r = requests.post(f"{NTS_BASE}/validate", params={"serviceKey": api_key}, json=body, timeout=timeout)
            r.raise_for_status()
            row = r.json()["data"][0]
            if row.get("valid") != "01":
                return _result("MISMATCH", "nts", d, note=row.get("valid_msg") or "대표자명 · 개업일자가 일치하지 않습니다")
            st = row.get("status") or {}
        else:
            r = requests.post(f"{NTS_BASE}/status", params={"serviceKey": api_key}, json={"b_no": [d]},
                              timeout=timeout)
            r.raise_for_status()
            st = r.json()["data"][0]
        code = NTS_CODE.get(st.get("b_stt_cd") or "", "UNREGISTERED")
        return _result(code, "nts", d, note=st.get("tax_type") or "", tax_type=st.get("tax_type"),
                       end_date=st.get("end_dt") or None)
    except Exception as e:  # noqa: BLE001 — 국세청 장애 · 키 오류 → 모의 판정으로 대신
        return {**first, "note": f"국세청 조회 실패 → 모의 판정 ({type(e).__name__})"}


def verify_business(value: str | None, *, api_key: str = "", owner: str | None = None,
                    start_date: str | None = None) -> dict:
    """가입 시 사업자 확인. 국세청 키가 있으면 실제 조회, 없으면 모의 판정."""
    if api_key:
        return nts_business_status(value or "", api_key, owner, start_date)
    return mock_business_status(value)


# ------------------------------------------------------------------ 사업자 진위확인 (화주 가입: 4개 일치)
# 시연용 모의 사업자 등록부: 번호 → 상호 · 대표자 · 개업일자. 이 번호들은 국세청 키가 있어도 등록부로 확인한다
# (실제 국세청은 실제 사업자만 확인해 주므로, 시연용 가짜 번호는 국세청에 보내면 항상 거절된다).
MOCK_REGISTRY = {
    "6178110004": {"name": "한빛물류", "owner": "김한빛", "start": "19960101"},
    "6178120007": {"name": "누리글로벌", "owner": "이누리", "start": "20010223"},
    "6178130000": {"name": "가온디스플레이", "owner": "박가온", "start": "19850217"},
    "1058712345": {"name": "배차테스트물류", "owner": "홍길동", "start": "20200115"},
}


def _company_key(name: str | None) -> str:
    """상호 비교용: 띄어쓰기 · (주) · 주식회사 · ㈜ 무시, 영문 대소문자 무시."""
    s = re.sub(r"\(주\)|㈜|주식회사", "", name or "")
    return re.sub(r"\s", "", s).lower()


def _identity_mismatch(record: dict, company: str, owner: str, start: str) -> list[str]:
    bad = []
    if _company_key(company) != _company_key(record["name"]):
        bad.append("기업명")
    if re.sub(r"\s", "", owner or "") != record["owner"]:
        bad.append("대표자명")
    if re.sub(r"\D", "", start or "") != record["start"]:
        bad.append("개업일자")
    return bad


def verify_business_identity(value: str | None, company: str | None, owner: str | None, start_date: str | None,
                             *, api_key: str = "", timeout: float = 5.0) -> dict:
    """사업자 진위확인: 사업자번호 · 상호 · 대표자명 · 개업일자 4개가 같은 사업자 등록 정보와 모두 일치해야 ok.
    순서: 형식 · Mod-10 → 휴업 · 폐업(모의) → 시연용 등록부(번호가 있으면) / 국세청 진위확인 API(키가 있으면).
    등록부에도 없고 키도 없으면 확인할 수 없으므로 거절한다. 국세청 장애면 통과시키지 않고 '확인 불가'로 거절."""
    d = normalize_biz_no(value)
    first = mock_business_status(d)
    if first["status"] in ("INVALID_FORMAT", "CHECKSUM_FAIL", "SUSPENDED", "CLOSED"):
        return first
    start = re.sub(r"\D", "", start_date or "")
    if d in MOCK_REGISTRY:
        bad = _identity_mismatch(MOCK_REGISTRY[d], company or "", owner or "", start)
        if bad:
            return _result("MISMATCH", "mock", d, note="사업자등록 정보와 다른 항목: " + " · ".join(bad))
        return _result("CONTINUING", "mock", d, note="기업명 · 대표자명 · 개업일자 일치 (시연용 등록부)")
    if not api_key:
        return _result("UNREGISTERED", "mock", d, note="시연용 사업자 등록부에 없는 번호입니다 (국세청 키가 없어 실제 조회 불가)")
    import requests
    try:
        body = {"businesses": [{"b_no": d, "start_dt": start, "p_nm": (owner or "").strip(),
                                "b_nm": (company or "").strip()}]}
        r = requests.post(f"{NTS_BASE}/validate", params={"serviceKey": api_key}, json=body, timeout=timeout)
        r.raise_for_status()
        row = r.json()["data"][0]
    except Exception as e:  # noqa: BLE001 — 국세청 장애 · 키 오류: 확인 없이 통과시키지 않는다
        return _result("UNAVAILABLE", "nts", d, note=f"국세청 진위확인 호출 실패 ({type(e).__name__})")
    if row.get("valid") != "01":
        return _result("MISMATCH", "nts", d,
                       note=row.get("valid_msg") or "기업명 · 대표자명 · 개업일자가 사업자등록 정보와 일치하지 않습니다")
    st = row.get("status") or {}
    code = NTS_CODE.get(st.get("b_stt_cd") or "", "UNREGISTERED")
    return _result(code, "nts", d, note="국세청 진위확인 일치", tax_type=st.get("tax_type"))


# ------------------------------------------------------------------ 차량 번호판
PLATE_RE = re.compile(r"^(?:([가-힣]{2})\s*)?(\d{2,3})\s*([가-힣])\s*(\d{4})$")
COMMERCIAL_CARGO = set("바사아자")   # 사업용 화물 (노란색 번호판)
COURIER = {"배"}                      # 택배 (노란색 번호판)
RENTAL = set("허하호")
PLATE_LABEL = {
    "COMMERCIAL_YELLOW": "사업용 (노란색 번호판)",
    "PRIVATE_WHITE": "자가용 (흰색 번호판)",
    "RENTAL": "렌터카",
    "INVALID": "번호판 형식 오류",
}


def classify_plate(plate: str | None) -> dict:
    """차량 번호 → plate_type. 예: '경기 88바 7712' → COMMERCIAL_YELLOW, '부산 11가 2233' → PRIVATE_WHITE."""
    m = PLATE_RE.match((plate or "").strip())
    if not m:
        t, char = "INVALID", None
    else:
        char = m.group(3)
        t = ("COMMERCIAL_YELLOW" if char in COMMERCIAL_CARGO | COURIER
             else "RENTAL" if char in RENTAL else "PRIVATE_WHITE")
    return {"plate_type": t, "label": PLATE_LABEL[t], "usage_char": char, "dispatchable": t == "COMMERCIAL_YELLOW"}


# ------------------------------------------------------------------ 톤수
STANDARD_TONNAGE = (1.0, 2.5, 5.0, 11.0, 25.0)


def tonnage(truck_type: str) -> float:
    """'2.5톤 카고' → 2.5, '11톤 냉동탑차' → 11.0 (못 읽으면 0)."""
    m = re.match(r"\s*(\d+(?:\.\d+)?)\s*톤", truck_type or "")
    return float(m.group(1)) if m else 0.0


# ------------------------------------------------------------------ 기사 가입 서류
REQUIRED_DRIVER_DOCS = {
    "license": "운전면허증 / 신분증",
    "cargo_license": "화물운송종사 자격증",
    "vehicle": "차량등록증 / 번호판 사진",
}


def missing_driver_docs(documents: list[dict]) -> list[str]:
    have = {d.get("kind") for d in documents or []}
    return [label for kind, label in REQUIRED_DRIVER_DOCS.items() if kind not in have]


def driver_compliance(plate: str, truck_type: str, documents: list[dict], biz: dict | None) -> dict:
    """기사 한 명의 컴플라이언스 요약 (가입 심사 화면 · 매칭 필터)."""
    p = classify_plate(plate)
    missing = missing_driver_docs(documents)
    issues = []
    if not p["dispatchable"]:
        issues.append(f"번호판: {p['label']} — 사업용 노란색 번호판만 배차 가능")
    if biz and not biz.get("ok"):
        issues.append(f"사업자: {biz.get('label')}")
    if missing:
        issues.append("서류 누락: " + ", ".join(missing))
    return {"plate_type": p["plate_type"], "plate_label": p["label"], "tonnage": tonnage(truck_type),
            "missing_docs": missing, "issues": issues, "ok": not issues}
