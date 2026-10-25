"""検索本文と引用を分け、原本文字範囲・件数・トークン予算を保持する。"""

import json
from collections import Counter
from copy import deepcopy

from sqlalchemy import select

from file_scan.profiles import digest
from search import schema as s
from file_scan.tokens import count_tokens, fit_prefix


def location(block, lo, hi, source_path):
    result = []
    for source in block["source_locations"]:
        safe = {k: deepcopy(v) for k, v in source.items() if k not in {"path", "source_uri", "url"}}
        safe["path"] = source_path
        if "start_line" in safe and block["content_text"]:
            body = block["content_text"]
            base = safe["start_line"]
            safe["start_line"] = base + body[:lo].count("\n")
            end_line = base + body[:hi].count("\n") - int(hi > 0 and body[hi - 1] == "\n")
            safe["end_line"] = min(safe.get("end_line", end_line), end_line)
        if safe not in result:
            result.append(safe)
    return result


def record(row, score, blocks, release_id, token_limit, *, role="hit", support=None):
    body = row["content_text"]
    excerpt = fit_prefix(body, token_limit)
    while excerpt and count_tokens(excerpt) > token_limit:
        excerpt = excerpt[:-1]
    if not excerpt.strip():
        return None, 0
    stop = len(excerpt)
    if stop < len(body) and any(
        blocks[(row["publication_id"], m["block_id"])]["element_type"] == "table"
        for m in row["mappings"]
    ):
        return None, 0
    spans, locations, block_ids, assets = [], [], [], []
    for mapping in row["mappings"]:
        start, end = mapping["chunk_char_start"], min(stop, mapping["chunk_char_end"])
        if end <= start:
            continue
        block = blocks[(row["publication_id"], mapping["block_id"])]
        lo = mapping["block_char_start"] + start - mapping["chunk_char_start"]
        hi = lo + end - start
        if excerpt[start:end] != (block["content_text"] or "")[lo:hi]:
            raise ValueError("invalid_frozen_provenance")
        spans.append(
            {
                "block_id": block["block_id"],
                "chunk_char_start": start,
                "chunk_char_end": end,
                "block_char_start": lo,
                "block_char_end": hi,
            }
        )
        block_ids.append(block["block_id"])
        for loc in location(block, lo, hi, row["source_path"]):
            if loc not in locations:
                locations.append(loc)
        for asset in block["assets"]:
            assets.append(
                {
                    "asset_id": asset["asset_id"],
                    "occurrence_id": asset["occurrence_id"],
                    "asset_role": asset["asset_role"],
                }
            )
    if not spans:
        return None, 0
    evidence_id = digest([row["generation_id"], row.get("chunk_id"), spans, "source"])
    metadata = {
        "evidence_id": evidence_id,
        "evidence_role": role,
        "document_id": row["document_id"],
        "generation_id": row["generation_id"],
        "publication_id": row["publication_id"],
        "source_version": row["source_version"],
        "conversion_revision": row["conversion_revision"],
        "block_ids": block_ids,
        "artifact_type": "source",
        "publication_state": "published",
        "quality_flags": row["quality_flags"],
        "target_release": release_id,
        "score_version": "rrf-bound-v1",
        "source_locations": locations,
        "source_spans": spans,
        "location_granularity": "block",
        "excerpted": stop < len(body),
        "related_evidence_ids": [],
        "related_assets": assets[:10],
        "source_ref": "/sources/" + row["publication_id"],
    }
    if row.get("chunk_id"):
        metadata["chunk_id"] = row["chunk_id"]
    if support:
        metadata["supporting_hit_ids"] = [support]
    return {
        "content": excerpt,
        "score": score,
        "title": row["title"],
        "metadata": metadata,
    }, count_tokens(excerpt)


async def assemble(connection, ranked, rows, release_id, setting, config):
    publication_ids = list({key[0] for key, _ in ranked})
    if not publication_ids:
        return [], [], "no_match"
    wanted = list(
        {(r["publication_id"], m["block_id"]) for r in rows.values() for m in r["mappings"]}
    )
    from sqlalchemy import tuple_

    source_blocks = {
        (r["publication_id"], r["block_id"]): dict(r)
        for r in (
            await connection.execute(
                select(s.blocks).where(
                    tuple_(s.blocks.c.publication_id, s.blocks.c.block_id).in_(wanted)
                )
            )
        ).mappings()
    }
    records, hits, limits = [], [], set()
    remaining = config.token_budget
    docs = Counter()
    reserved = min(5, setting.top_k // 3)
    pending = []
    for key, score in ranked:
        if score < setting.score_threshold:
            continue
        row = rows[key]
        if docs[row["document_id"]] >= 3:
            limits.add("document_cap")
            continue
        if len(hits) >= setting.top_k - reserved:
            pending.append((key, score))
            continue
        item, used = record(
            row, score, source_blocks, release_id, min(config.record_token_budget, remaining)
        )
        if not item:
            limits.add("token_budget")
            continue
        records.append(item)
        hits.append((row, item))
        docs[row["document_id"]] += 1
        remaining -= used
        if item["metadata"]["excerpted"]:
            limits.add("token_budget")
    # 親・確認済み関係・隣接を1段だけ取得。全ブロックやワークブックへ展開しない。
    seen = {
        (row["publication_id"], bid) for row, item in hits for bid in item["metadata"]["block_ids"]
    }
    context_count = 0
    hit_publications = {r["publication_id"] for r, _ in hits}
    publications = (
        {
            r["publication_id"]: r
            for r in (
                await connection.execute(
                    select(s.publications.c.publication_id, s.publications.c.relations).where(
                        s.publications.c.publication_id.in_(hit_publications)
                    )
                )
            ).mappings()
        }
        if reserved
        else {}
    )
    for row, hit in hits:
        if context_count >= reserved or remaining <= 0:
            break
        hit_blocks = [
            source_blocks[(row["publication_id"], bid)] for bid in hit["metadata"]["block_ids"]
        ]
        linked = [
            r["to_block_id"]
            for r in publications[row["publication_id"]]["relations"]
            if r["from_block_id"] in hit["metadata"]["block_ids"]
        ]
        ids = [b["parent_id"] for b in hit_blocks if b["parent_id"]] + linked
        choices = []
        if ids:
            choices.extend(
                (
                    await connection.execute(
                        select(s.blocks)
                        .where(
                            s.blocks.c.publication_id == row["publication_id"],
                            s.blocks.c.block_id.in_(ids),
                        )
                        .order_by(s.blocks.c.reading_order)
                        .limit(2)
                    )
                ).mappings()
            )
        choices = [b for b in choices if b["content_text"]]
        base = hit_blocks[-1]
        if len(choices) < 2 and base["section_path"]:
            choices.extend(
                (
                    await connection.execute(
                        select(s.blocks)
                        .where(
                            s.blocks.c.publication_id == row["publication_id"],
                            s.blocks.c.element_type == "heading",
                            s.blocks.c.section_path == base["section_path"],
                            s.blocks.c.reading_order < base["reading_order"],
                        )
                        .order_by(s.blocks.c.reading_order.desc())
                        .limit(2 - len(choices))
                    )
                ).mappings()
            )
        if len(choices) < 2:
            # 表ヘッダーのセル座標は保存済みの根拠から元の行ブロックへたどる。
            import re

            cells = [
                value.split(":", 1)[0]
                for value in base["content_data"].get("headers", [])
                if isinstance(value, str) and re.match(r"^[A-Z]+[0-9]+:", value)
            ][:16]
            if cells:
                from sqlalchemy import or_

                choices.extend(
                    (
                        await connection.execute(
                            select(s.blocks)
                            .where(
                                s.blocks.c.publication_id == row["publication_id"],
                                s.blocks.c.parent_id == base["parent_id"],
                                or_(
                                    *(
                                        s.blocks.c.content_data["cells"].contains(
                                            [{"coordinate": cell}]
                                        )
                                        for cell in cells
                                    )
                                ),
                            )
                            .order_by(s.blocks.c.reading_order)
                            .limit(2 - len(choices))
                        )
                    ).mappings()
                )
        if len(choices) < 2:
            choices.extend(
                (
                    await connection.execute(
                        select(s.blocks)
                        .where(
                            s.blocks.c.publication_id == row["publication_id"],
                            s.blocks.c.parent_id == base["parent_id"]
                            if base["parent_id"]
                            else s.blocks.c.parent_id.is_(None),
                            s.blocks.c.section_path == base["section_path"],
                            s.blocks.c.reading_order > base["reading_order"],
                        )
                        .order_by(s.blocks.c.reading_order)
                        .limit(2 - len(choices))
                    )
                ).mappings()
            )
        for block in choices[:2]:
            key = (row["publication_id"], block["block_id"])
            if key in seen or not block["content_text"]:
                continue
            if context_count >= reserved or remaining <= 0:
                limits.add("context_limit")
                break
            source_blocks[key] = dict(block)
            contextual = row | {
                "chunk_id": None,
                "content_text": block["content_text"],
                "mappings": [
                    {
                        "block_id": block["block_id"],
                        "chunk_char_start": 0,
                        "chunk_char_end": len(block["content_text"]),
                        "block_char_start": 0,
                        "block_char_end": len(block["content_text"]),
                    }
                ],
                "quality_flags": block["quality_flags"],
            }
            item, used = record(
                contextual,
                hit["score"],
                source_blocks,
                release_id,
                min(config.record_token_budget, remaining),
                role="context",
                support=hit["metadata"]["evidence_id"],
            )
            if item:
                records.append(item)
                remaining -= used
                context_count += 1
                seen.add(key)
                hit["metadata"]["related_evidence_ids"].append(item["metadata"]["evidence_id"])
    for key, score in pending:
        row = rows[key]
        if len(records) >= setting.top_k:
            limits.add("top_k")
            break
        if docs[row["document_id"]] >= 3:
            limits.add("document_cap")
            continue
        item, used = record(
            row, score, source_blocks, release_id, min(config.record_token_budget, remaining)
        )
        if item:
            records.append(item)
            docs[row["document_id"]] += 1
            remaining -= used
        else:
            limits.add("token_budget")
    # JSON全体の上限はstatus分も含め、service側で最終検査する。
    records.sort(key=lambda r: (-r["score"], r["metadata"]["evidence_id"]))
    qualified = any(score >= setting.score_threshold for _, score in ranked)
    return (
        records,
        sorted(limits),
        "matched" if records else "budget_exhausted" if qualified else "no_match",
    )


def fit_response(result, max_bytes):
    """参照が切れた文脈も除去してからJSONサイズを再検査する。"""
    status = result["retrieval_status"]
    while len(json.dumps(result, ensure_ascii=False).encode()) > max_bytes and result["records"]:
        contexts = [r for r in result["records"] if r["metadata"]["evidence_role"] == "context"]
        result["records"].remove(contexts[-1] if contexts else result["records"][-1])
        status["limit_reasons"] = sorted(set(status["limit_reasons"]) | {"response_size"})
        ids = {r["metadata"]["evidence_id"] for r in result["records"]}
        result["records"] = [
            r
            for r in result["records"]
            if r["metadata"]["evidence_role"] == "hit"
            or all(support in ids for support in r["metadata"].get("supporting_hit_ids", []))
        ]
        ids = {r["metadata"]["evidence_id"] for r in result["records"]}
        for r in result["records"]:
            r["metadata"]["related_evidence_ids"] = [
                i for i in r["metadata"]["related_evidence_ids"] if i in ids
            ]
    status["returned_records"] = len(result["records"])
    if status["result_state"] == "matched" and not result["records"]:
        status["result_state"] = "budget_exhausted"
    return result
