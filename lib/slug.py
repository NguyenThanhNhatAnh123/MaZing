"""Campaign URL slugs.

From the design FILE NAME first (its first part when it is a code: letters + digits, or
3+ digits - "ge123 front.png" -> "ge123", "216.png" -> "216"); else from the title
("ICH KAM ICH SAH" -> "ich-kam-ich-sah", like Mayzing does). A slug already used by an
exported batch (used_slugs.txt), by another card or by a campaign of the store gets a
suffix: a letter for codes (ge123a), "-2" for word slugs.
"""
from __future__ import annotations

import re
import string
import unicodedata

import config

_CODE_SPLIT = re.compile(r"[\s_()\[\]]+")
_VALID = re.compile(r"^[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*$")
MAX_LEN = 80
MIN_DIGITS_ONLY = 3
_FOLD = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "Ä": "ae", "Ö": "oe", "Ü": "ue",
         "&": " and ", "'": "", "’": ""}


def is_valid(s: str) -> bool:
    return bool(_VALID.match(s or "")) and len(s) <= MAX_LEN and s.lower() not in config.BLOCKED_SLUGS


def clean(text: str) -> str:
    """Typed / pasted text -> slug ("https://shop.com/ab123" -> "ab123", "Ab 12" -> "ab-12")."""
    text = (text or "").strip()
    if "/" in text:
        text = text.rstrip("/").rsplit("/", 1)[-1]
    text = text.split("?")[0]
    return slugify(text, keep_case=False)


def from_filename(stem: str) -> str:
    parts = [p for p in _CODE_SPLIT.split(stem.strip().lower()) if p]
    first = parts[0] if parts else ""
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", first) or not re.search(r"\d", first):
        return ""
    if first.isdigit() and len(first) < MIN_DIGITS_ONLY:
        return ""
    if not re.search(r"[a-z]", first) and not first.isdigit():
        return ""
    return first[:MAX_LEN]


def slugify(text: str, keep_case: bool = False) -> str:
    s = "".join(_FOLD.get(c, c) for c in (text or ""))
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    if not keep_case:
        s = s.lower()
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-")
    if len(s) > MAX_LEN:
        s = s[:MAX_LEN].rsplit("-", 1)[0] if "-" in s[:MAX_LEN] else s[:MAX_LEN]
    return s


def _suffixes(slug: str):
    if re.search(r"\d$", slug) and "-" not in slug:     # a code: ge123 -> ge123a, ge123b
        letters = string.ascii_lowercase
        for a in letters:
            yield slug + a
        for a in letters:
            for b in letters:
                yield slug + a + b
    else:                                                 # words: -2, -3, ...
        for n in range(2, 1000):
            yield f"{slug}-{n}"


def load_used() -> set[str]:
    try:
        return {line.strip().lower() for line in config.USED_SLUGS_FILE.read_text(encoding="utf-8").splitlines()
                if line.strip()}
    except OSError:
        return set()


def commit(slugs: list[str]) -> None:
    """Remember exported slugs for ever (never reused by a later batch)."""
    if not slugs:
        return
    with config.USED_SLUGS_FILE.open("a", encoding="utf-8") as f:
        for s in slugs:
            f.write(s.strip().lower() + "\n")


def unique(wanted: str, taken_local: set[str], is_taken_remote=None) -> str:
    """First free variant of `wanted`. taken_local: lower-case slugs of the history and the
    other cards; is_taken_remote(slug) -> bool | None (None = could not ask)."""
    def taken(s: str) -> bool:
        if s.lower() in taken_local:
            return True
        if is_taken_remote is not None:
            return bool(is_taken_remote(s))
        return False

    if not taken(wanted):
        return wanted
    for cand in _suffixes(wanted):
        if not taken(cand):
            return cand
    return wanted
