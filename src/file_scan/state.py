"""現在の選択と履歴を分ける共通クエリ。最新作成日時へ暗黙に戻らない。"""

from sqlalchemy import Text, and_, case, func, or_, select

from file_scan.db import tables


def input_hash_sql(connection, column):
    if connection.dialect.name == "sqlite":
        return func.file_scan_input_hash(column)
    # pipeline.digest(str)と同じJSON文字列のSHA-256。本文全件をPythonへ読まない。
    return "sha256:" + func.encode(func.sha256(func.convert_to(
        func.to_json(column).cast(Text), "UTF8")), "hex")


def current_documents(knowledge_id, revisions=None):
    members, docs = tables["knowledge_documents"], tables["documents"]
    selections, generations = tables["selected_generations"], tables["extraction_generations"]
    matches = True if revisions is None else or_(False, *(
        and_(docs.c.format == fmt, generations.c.conversion_revision == revision)
        for fmt, revision in revisions.items()))
    status = case(
        (or_(members.c.collection_status != "available", docs.c.source_status != "available"), "unavailable"),
        (selections.c.generation_id.is_(None), case((members.c.last_error_code.is_not(None), "failed"), else_="pending")),
        (generations.c.source_version != docs.c.latest_source_version, "stale"),
        (or_(generations.c.conversion_revision != selections.c.conversion_revision, ~matches
             if not isinstance(matches, bool) else False), "config_mismatch"),
        (generations.c.processing_status == "succeeded", "succeeded"),
        (generations.c.processing_status == "partial", "partial"),
        (generations.c.processing_status == "failed", "failed"),
        else_="pending",
    )
    return select(docs.c.document_id, generations.c.generation_id, status.label("state")).select_from(
        members.join(docs, docs.c.document_id == members.c.document_id).outerjoin(selections, and_(
            selections.c.knowledge_id == members.c.knowledge_id,
            selections.c.document_id == members.c.document_id)).outerjoin(generations, and_(
            generations.c.generation_id == selections.c.generation_id,
            generations.c.document_id == docs.c.document_id))
    ).where(members.c.knowledge_id == knowledge_id, members.c.included.is_(True)).subquery()


def current_chunks(knowledge_id, revisions=None):
    documents = current_documents(knowledge_id, revisions)
    chunks = tables["chunks"]
    return chunks.join(documents, and_(documents.c.generation_id == chunks.c.generation_id,
                                      documents.c.state.in_(("succeeded", "partial"))))
