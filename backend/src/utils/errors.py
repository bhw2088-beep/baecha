"""API 오류 (main.py 의 exception handler 가 JSON 응답으로 바꾼다)."""


class ApiError(Exception):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
