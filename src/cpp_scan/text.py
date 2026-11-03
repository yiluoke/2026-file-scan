"""UTF-8／CP932と原本位置。置換デコードで根拠を失わない。"""

from __future__ import annotations

import fnmatch
import re

from .store import ScanError, bytes_hash

# コメント、通常文字列、C++のraw stringを同時に認識して位置を保持する。
TOKENS = re.compile(
    r'R"(?P<delimiter>[^ ()\\\t\r\n]{0,16})\(.*?\)(?P=delimiter)"'
    r'|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\''
    r'|//[^\r\n]*|/\*.*?\*/', re.DOTALL,
)


def masked(text: str, *, strings: bool = True) -> str:
    """行数と文字位置を変えずにコメント・文字列を空白へ置き換える。"""
    def replacement(match):
        if not strings and not match.group().startswith(("//", "/*")):
            return match.group()
        return re.sub(r"[^\r\n]", " ", match.group())
    return TOKENS.sub(replacement, text)


def literals(text: str):
    for match in TOKENS.finditer(text):
        if match.group().startswith('"'):
            yield match.start(), match.group()[1:-1]


def decode(data: bytes, path: str, settings) -> tuple[str, dict]:
    requested = settings.source_encoding
    for pattern, encoding in settings.encoding_overrides.items():
        if fnmatch.fnmatchcase(path, pattern):
            requested = encoding
    warnings = []
    if requested == "auto":
        try:
            text = data.decode("utf-8-sig")
            encoding = "utf-8"
            if any(b >= 128 for b in data) and not data.startswith(b"\xef\xbb\xbf"):
                try:
                    if data.decode("cp932") != text:
                        warnings.append("encoding_ambiguous_utf8_preferred")
                except UnicodeDecodeError:
                    pass
        except UnicodeDecodeError:
            encoding = settings.fallback_encoding
            try:
                text = data.decode(encoding)
            except UnicodeDecodeError:
                raise ScanError("source_decode_failed") from None
            warnings.append("fallback_encoding_used")
    else:
        encoding = requested
        try:
            text = data.decode("utf-8-sig" if encoding == "utf-8" else encoding)
        except UnicodeDecodeError:
            raise ScanError("source_decode_failed") from None
    if "\x00" in text:
        raise ScanError("binary_or_unsupported_encoding")
    # 改行をそのまま保持するため、行番号も原本に一致する。
    lines = []
    raw_offset = 3 if encoding == "utf-8" and data.startswith(b"\xef\xbb\xbf") else 0
    utf8_offset = 0
    for number, line in enumerate(text.splitlines(keepends=True), 1):
        raw_length, utf8_length = len(line.encode(encoding)), len(line.encode("utf-8"))
        lines.append({"line": number, "source_start": raw_offset,
                      "source_end": raw_offset + raw_length, "utf8_start": utf8_offset,
                      "utf8_end": utf8_offset + utf8_length})
        raw_offset += raw_length
        utf8_offset += utf8_length
    return text, {"encoding": encoding, "encoding_requested": requested,
                  "warnings": warnings, "line_count": len(lines), "lines": lines,
                  "utf8_sha256": bytes_hash(text.encode("utf-8"))}


def calls(text: str, names: str):
    """釣り合う括弧を持つ呼出式だけを返す。解決済み呼出グラフとは扱わない。"""
    code = masked(text)
    for match in re.finditer(r"\b(" + names + r")\s*\(", code):
        opening = match.end() - 1
        depth = 1
        end = opening + 1
        while end < len(code) and depth:
            if code[end] == "(":
                depth += 1
            elif code[end] == ")":
                depth -= 1
            end += 1
        if not depth:
            yield match.group(1), match.start(), end, text[opening + 1:end - 1]


def arguments(value: str) -> list[str]:
    code = masked(value)
    result, start, depth = [], 0, 0
    for i, char in enumerate(code):
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "," and depth == 0:
            result.append(value[start:i].strip())
            start = i + 1
    return [*result, value[start:].strip()]
