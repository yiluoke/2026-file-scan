"""原文・設定値を反射しない検索APIのエラー。"""

ERRORS = {
    "UNAUTHENTICATED": (401, 1002, "接続認証に失敗しました。"),
    "SCOPE_NOT_FOUND": (404, 2001, "検索範囲または対象版が見つかりません。"),
    "RELEASE_UNRESOLVED": (409, 3001, "既定の対象版が設定されていません。"),
    "COVERAGE_NOT_COMPLETE": (409, 3002, "検索対象の準備が完了していません。"),
    "RESULT_BUDGET_EXHAUSTED": (409, 3003, "検索結果の返却予算が不足しています。"),
    "REQUEST_TOO_LARGE": (413, 4001, "要求サイズの上限を超えています。"),
    "UNSUPPORTED_MEDIA_TYPE": (415, 4002, "JSON形式で要求してください。"),
    "INVALID_REQUEST": (422, 4003, "検索条件の形式または値が不正です。"),
    "RATE_LIMITED": (429, 5001, "同時検索数の上限に達しています。"),
    "SEARCH_UNAVAILABLE": (503, 5002, "検索処理を利用できません。"),
    "SEARCH_TIMEOUT": (504, 5003, "検索処理が制限時間を超えました。"),
    "INTERNAL_ERROR": (500, 9001, "検索処理でエラーが発生しました。"),
}


class SearchError(Exception):
    def __init__(self, code, *, retryable=False):
        self.code = code
        self.status, self.number, self.message = ERRORS[code]
        self.retryable = retryable
        super().__init__(code)

    def response(self, request_id):
        return {
            "error_code": self.number,
            "error_msg": self.message,
            "code": self.code,
            "request_id": request_id,
            "retryable": self.retryable,
        }
