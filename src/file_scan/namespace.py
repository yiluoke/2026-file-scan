"""PostgreSQLの物理配置先。SQLiteでは名前空間を既定DBへ変換する。"""

import re

APPLICATION_SCHEMA = "winnap"


def validate_namespace(schema: str) -> str:
    # 接続オプションへ入れる識別子は、固定名またはテスト用の単純名だけに限定する。
    if not isinstance(schema, str) or not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", schema):
        raise ValueError("スキーマ名の形式が不正です")
    return schema


def connection_arguments(schema=APPLICATION_SCHEMA, arguments=None):
    schema = validate_namespace(schema)
    values = dict(arguments or {})
    options = values.get("options", "")
    values["options"] = f"{options} -csearch_path={schema},public".strip()
    return values


def database_schema(connection):
    if connection.dialect.name == "sqlite":
        return None
    return connection.get_execution_options().get("schema_translate_map", {}).get(
        APPLICATION_SCHEMA, APPLICATION_SCHEMA)
