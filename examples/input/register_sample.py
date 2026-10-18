"""解析用の架空ソース。取り込みツールはこのコードを実行しません。"""


def validate_user_id(user_id: str) -> bool:
    """検証用サンプル: IDが英数字かつ1〜20文字ならTrueを返す。"""
    return user_id.isascii() and user_id.isalnum() and 1 <= len(user_id) <= 20
