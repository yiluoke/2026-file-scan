"""原文・設定値を反射しない検索APIのエラーと処理段階。"""

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from time import monotonic


class SearchStage(Enum):
    REQUEST_BODY = "リクエスト本文の受信"
    VALIDATION = "検索条件の検証"
    SEARCH_SLOT = "同時検索枠の取得"
    DB_CONNECTION = "DB接続の取得"
    SEARCH_SCOPE = "検索対象と公開状態の確認"
    QUERY_EMBEDDING = "質問のベクトル化（OpenAI）"
    VECTOR_SEARCH = "pgvectorでのベクトル検索"
    FULLTEXT_SEARCH = "PGroongaでの全文検索"
    IDENTIFIER_SEARCH = "識別子の検索"
    EVIDENCE = "検索結果と根拠の組み立て"
    FRESHNESS_CHECK = "公開状態の最終確認"
    EVENT_WRITE = "検索記録の書き込み"
    RESPONSE = "検索応答の準備"


@dataclass
class SearchTrace:
    stage: SearchStage = SearchStage.REQUEST_BODY
    started: float = field(default_factory=monotonic)
    http_deadline: asyncio.Timeout | None = None

    def diagnostics(self):
        return {
            "stage": self.stage.name.lower(),
            "elapsed_ms": int((monotonic() - self.started) * 1000),
        }


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
    def __init__(self, code, *, retryable=False, timeout_kind=None):
        self.code = code
        self.status, self.number, self.message = ERRORS[code]
        self.retryable = retryable
        self.timeout_kind = timeout_kind
        super().__init__(code)

    def response(self, request_id):
        return {
            "error_code": self.number,
            "error_msg": self.message,
            "code": self.code,
            "request_id": request_id,
            "retryable": self.retryable,
        }
