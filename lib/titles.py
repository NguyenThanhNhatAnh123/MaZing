"""Short campaign titles from the start of the quote; keep the full quote separately."""
from __future__ import annotations

import re

import config


def _patterns() -> list[tuple[str, re.Pattern[str]]]:
    out = []
    for phrase in config.FORBIDDEN_WORDS:
        body = r"[\s\-_.]+".join(re.escape(w) for w in phrase.split())
        out.append((phrase, re.compile(rf"(?<![\w]){body}(?![\w])", re.IGNORECASE)))
    return out


def tidy(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "")
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    return text.strip(" -,")


def build_title(quote: str) -> tuple[str, list[str]]:
    """(title, flags). The quote keeps its line breaks; the title is one line."""
    flags: list[str] = []
    text = " ".join(line.strip() for line in (quote or "").splitlines() if line.strip())
    for phrase, pat in _patterns():
        if pat.search(text):
            text = pat.sub(" ", text)
            flags.append(f"FORBIDDEN_REMOVED:{phrase}")
    text = tidy(text)
    if (config.TITLE_CASE or "upper").lower() == "upper":
        text = text.upper()
    original = text
    text = " ".join(text.split()[:config.TITLE_MAX_WORDS])
    if len(text) > config.TITLE_MAX_LENGTH:
        cut = text[:config.TITLE_MAX_LENGTH]
        if not text[config.TITLE_MAX_LENGTH].isspace() and " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        text = cut
    if text != original:
        text = text.rstrip(" -,:;")
        flags.append("TITLE_SHORTENED")
    if not text:
        flags.append("NO_TITLE")
    return text, flags
