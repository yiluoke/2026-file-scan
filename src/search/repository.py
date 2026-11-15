"""同一スナップショットで予定原本・採用済み候補・検索経路を読む。"""

from sqlalchemy import and_, func, literal_column, or_, select, text

from file_scan.db import tables as core
from search import schema as s
from search.errors import SearchError
from search.ranking import identifiers


async def catalog(connection, knowledge_id):
    row = (
        (
            await connection.execute(
                select(
                    s.catalogs,
                    core["knowledge_bases"].c.catalog_revision,
                    core["knowledge_bases"].c.scan_status,
                )
                .join(core["knowledge_bases"])
                .where(s.catalogs.c.knowledge_id == knowledge_id, s.catalogs.c.enabled.is_(True))
            )
        )
        .mappings()
        .first()
    )
    if not row:
        raise SearchError("SCOPE_NOT_FOUND")
    return dict(row)


async def resolve(connection, request, knowledge_id, *, allow_history=False):
    if request.knowledge_id != knowledge_id:
        raise SearchError("SCOPE_NOT_FOUND")
    config = await catalog(connection, knowledge_id)
    filters = request.filters()
    if "feature_id" in filters and not config["feature_filter_enabled"]:
        raise SearchError("INVALID_REQUEST")
    target = filters.get("target_release", "current")
    if target == "current":
        release_id, mode = config["default_release_id"], "current"
        if not release_id:
            raise SearchError("RELEASE_UNRESOLVED")
        row = (
            (
                await connection.execute(
                    select(s.releases).where(
                        s.releases.c.release_id == release_id,
                        s.releases.c.knowledge_id == knowledge_id,
                    )
                )
            )
            .mappings()
            .first()
        )
    else:
        mode = "history"
        if not allow_history:
            raise SearchError("SCOPE_NOT_FOUND")
        row = (
            (
                await connection.execute(
                    select(s.releases)
                    .where(
                        s.releases.c.knowledge_id == knowledge_id,
                        or_(s.releases.c.release_id == target, s.releases.c.name == target),
                    )
                    .order_by((s.releases.c.release_id == target).desc())
                )
            )
            .mappings()
            .first()
        )
    if not row:
        raise SearchError("SCOPE_NOT_FOUND")
    return config | {"release_id": row["release_id"], "search_mode": mode}


async def expected(connection, scope, *, document_id=None):
    docs, members, selected, gens = (
        core[n]
        for n in (
            "documents",
            "knowledge_documents",
            "selected_generations",
            "extraction_generations",
        )
    )
    graph = members.join(docs).outerjoin(
        s.bindings,
        and_(
            s.bindings.c.document_id == docs.c.document_id,
            s.bindings.c.release_id == scope["release_id"],
        ),
    )
    graph = graph.outerjoin(
        s.publications, s.publications.c.publication_id == s.bindings.c.publication_id
    )
    graph = graph.outerjoin(
        selected,
        and_(
            selected.c.document_id == docs.c.document_id,
            selected.c.knowledge_id == members.c.knowledge_id,
        ),
    )
    graph = graph.outerjoin(gens, gens.c.generation_id == selected.c.generation_id)
    query = (
        select(
            docs.c.document_id,
            docs.c.source_status,
            docs.c.latest_source_version,
            members.c.collection_status,
            members.c.last_error_code,
            selected.c.generation_id.label("selected_generation_id"),
            selected.c.conversion_revision.label("selected_revision"),
            gens.c.processing_status,
            gens.c.updated_at.label("selected_updated_at"),
            s.bindings.c.release_id.label("binding_release_id"),
            s.bindings.c.expected_source_version,
            s.bindings.c.expected_conversion_revision,
            s.publications.c.publication_id,
            s.publications.c.generation_id,
            s.publications.c.source_version,
            s.publications.c.conversion_revision,
            s.publications.c.generation_updated_at,
            s.publications.c.partial,
            s.publications.c.embedding_profile_id,
        )
        .select_from(graph)
        .where(members.c.knowledge_id == scope["knowledge_id"], members.c.included.is_(True))
    )
    if scope["search_mode"] == "history":
        query = query.where(s.bindings.c.release_id == scope["release_id"])
    if document_id is not None:
        query = query.where(docs.c.document_id == document_id)
    return [
        dict(r) for r in (await connection.execute(query.order_by(docs.c.document_id))).mappings()
    ]


def coverage(rows, scope):
    counts = dict(
        pending_updates=0,
        failed_documents=0,
        partial_documents=0,
        uncollected_documents=0,
        index_pending_documents=0,
        unknown_documents=0,
    )
    eligible = []
    for row in rows:
        if row["source_status"] != "available" or row["collection_status"] == "failed":
            counts["failed_documents"] += 1
        elif row["collection_status"] != "available":
            counts["uncollected_documents"] += 1
        elif not row["binding_release_id"]:
            counts["unknown_documents"] += 1
        elif scope["search_mode"] == "current" and row["processing_status"] == "failed":
            counts["failed_documents"] += 1
        elif not row["publication_id"]:
            counts["pending_updates"] += 1
        elif row["embedding_profile_id"] != scope["embedding_profile_id"]:
            counts["pending_updates"] += 1
        elif (
            row["expected_source_version"] != row["source_version"]
            or row["expected_conversion_revision"] != row["conversion_revision"]
        ):
            counts["pending_updates"] += 1
        elif scope["search_mode"] == "current" and (
            row["latest_source_version"] != row["source_version"]
            or row["selected_generation_id"] != row["generation_id"]
            or row["selected_revision"] != row["conversion_revision"]
            or row["selected_updated_at"] != row["generation_updated_at"]
        ):
            counts["pending_updates"] += 1
        else:
            eligible.append(row["publication_id"])
            counts["partial_documents"] += bool(row["partial"])
    return counts, eligible


def candidate_filter(publication_ids, filters):
    clauses = [s.chunks.c.publication_id.in_(publication_ids)]
    if "document_id" in filters:
        values = filters["document_id"]
        clauses.append(
            s.publications.c.document_id.in_(values if isinstance(values, list) else [values])
        )
    if "feature_id" in filters:
        values = filters["feature_id"]
        clauses.append(
            s.chunks.c.feature_ids.overlap(values if isinstance(values, list) else [values])
        )
    if "artifact_type" in filters:
        values = filters["artifact_type"]
        origin = func.coalesce(s.chunks.c.mappings[0]["artifact_type"].astext, "source")
        clauses.append(origin.in_(values if isinstance(values, list) else [values]))
    return and_(*clauses)


async def has_candidates(connection, publication_ids, filters):
    if not publication_ids:
        return False
    candidate = (
        select(s.chunks.c.chunk_id)
        .select_from(s.chunks.join(s.publications))
        .where(candidate_filter(publication_ids, filters))
    )
    return (await connection.execute(select(candidate.exists()))).scalar_one()


async def lanes(connection, query, vector, publication_ids, filters, limit, *, semantic_minimum):
    graph = s.chunks.join(s.publications)
    allowed = candidate_filter(publication_ids, filters)
    stable = [s.publications.c.generation_id, s.chunks.c.chunk_id, s.chunks.c.publication_id]
    key = [s.chunks.c.publication_id, s.chunks.c.chunk_id]
    distance = s.chunks.c.embedding.cosine_distance(vector)
    max_distance = 1.0 - semantic_minimum
    similarity_matches = distance < 1.0 if max_distance == 1.0 else distance <= max_distance
    dense = (
        await connection.execute(
            select(*key)
            .select_from(graph)
            .where(allowed, s.chunks.c.embedding.is_not(None), similarity_matches)
            .order_by(distance, *stable)
            .limit(limit)
        )
    ).all()
    # Groonga自身で語へ分け、検索式として解釈させず単語検索のバインド値にする。
    tokens = (
        await connection.execute(
            text(
                "SELECT pgroonga_tokenize(:query, 'tokenizer', 'TokenBigram', 'normalizer', 'NormalizerAuto')"
            ),
            {"query": query},
        )
    ).scalar_one()
    terms = []
    for token in tokens:
        word = token["value"]
        if len(word) >= 2 and len(word) <= 128 and word.strip() and word not in terms:
            terms.append(word)
        if len(terms) == 16:
            break
    lexical = []
    if terms:
        matches = or_(*(s.chunks.c.search_text.op("&@")(word) for word in terms))
        lexical = (
            await connection.execute(
                select(*key)
                .select_from(graph)
                .where(allowed, matches)
                .order_by(
                    func.pgroonga_score(
                        literal_column("search_chunks.tableoid"),
                        literal_column("search_chunks.ctid"),
                    ).desc(),
                    *stable,
                )
                .limit(limit)
            )
        ).all()
    names = identifiers(query)
    exact = []
    if names:
        # 単純な完全一致数。文字列は配列値としてbindされ、SQL構文へ展開しない。
        from sqlalchemy import case

        score = sum(case((s.chunks.c.identifiers.contains([name]), 1), else_=0) for name in names)
        exact = (
            await connection.execute(
                select(*key)
                .select_from(graph)
                .where(allowed, s.chunks.c.identifiers.overlap(names))
                .order_by(score.desc(), *stable)
                .limit(limit)
            )
        ).all()
    return [[tuple(r) for r in lane] for lane in (dense, lexical, exact)]


async def hydrate(connection, keys):
    from sqlalchemy import tuple_

    if not keys:
        return {}
    rows = (
        await connection.execute(
            select(
                s.chunks,
                s.publications.c.document_id,
                s.publications.c.generation_id,
                s.publications.c.source_version,
                s.publications.c.conversion_revision,
                s.publications.c.source_path,
            )
            .join(s.publications)
            .where(tuple_(s.chunks.c.publication_id, s.chunks.c.chunk_id).in_(keys))
        )
    ).mappings()
    return {(r["publication_id"], r["chunk_id"]): dict(r) for r in rows}
