"""大小文字・記号を保つ識別子抽出と、固定上限でのRRFスコア。"""

import re


def identifiers(query, *, limit=16):
    quoted = re.findall(r"`([^`\n]+)`", query)
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_.:/\\-]*", query)
    result = []
    for token in quoted + tokens:
        token = token.rstrip(".:/")
        if 1 <= len(token) <= 256 and token not in result:
            result.append(token)
    return result[:limit]


def fuse(lanes):
    scores = {}
    for lane in lanes:
        seen = set()
        for key in lane:
            if key in seen:
                continue
            seen.add(key)
            scores[key] = scores.get(key, 0.0) + 1 / (60 + len(seen))
    return sorted(
        ((key, min(1.0, value / (3 / 61))) for key, value in scores.items()),
        key=lambda pair: (-pair[1], pair[0]),
    )
