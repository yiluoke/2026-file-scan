"""Embedding入力の共通トークン計測。原本文字列の座標は変更しない。"""

from functools import lru_cache

import tiktoken


@lru_cache(maxsize=1)
def encoding():
    return tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    return len(encoding().encode(text, disallowed_special=()))


def fit_prefix(text: str, budget: int) -> str:
    """Unicode文字境界で検索用文脈を収める。元ブロックは切り捨てない。"""
    if count_tokens(text) <= budget:
        return text
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if count_tokens(text[:middle]) <= budget:
            low = middle
        else:
            high = middle - 1
    return text[:low]
