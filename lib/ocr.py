"""Read the printed quote of a design with Claude vision (becomes the campaign title).

CLAUDE_MODEL (default Haiku 5.5) reads every design. When it is not sure (confidence not
"high", no text, or an error that is not the key / credit) CLAUDE_HARD_MODEL (default
Opus 5.5, effort high, server-side refusal fallback) reads it again with the first read
as a hint. No ANTHROPIC_API_KEY -> no reading, titles are typed on the review card.
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

import config
from lib import artwork

_PLACEHOLDER_KEYS = {"", "sk-ant-...", "your-key-here"}

SYSTEM_PROMPT = """You transcribe the quote printed on graphic t-shirt designs for a print-on-demand seller.
This is design artwork, not a document: the lettering is stylised (script, distressed, outlined,
arched), set in separate blocks and mixed with illustrations. Quotes can be in any language
(English, German, French, ...).

Return ONLY a JSON object: {"quote": "...", "confidence": "high|medium|low"}

"quote" is every word of the design's quote in the order a person would read it aloud. Keep the
printed line breaks as "\\n" between lines, single spaces within a line. Do not shorten it.

Reading order:
- The quote is made of text BLOCKS. Finish one block before starting the next.
- A small line under or next to a big word belongs to that word.
- Text blocks LEFT and RIGHT of a central illustration are two columns: read the whole left
  column top to bottom, then the whole right column. Never jump back and forth.
- Lines across the full width are read in their place from top to bottom.

Leave out:
- Text that belongs to the illustration (words on a character's clothing, a sign, a label).
- Decoration (stars, lines, hearts, icons), brand tags, size labels, watermarks.

Exact copy:
- Copy every word letter by letter as printed, in its own language, with umlauts and accents
  (Ä Ö Ü ß é ...), apostrophes, numbers and censored words such as "SH*T".
- For script fonts look at the letter shapes one by one. Never swap a word for a more common
  or similar one, never translate, never correct the wording, add no punctuation.

Confidence: "high" only when every word is certain and the order is unambiguous; "medium" when a
word or the block order is uncertain; "low" when part of the text is unreadable.
No quote at all: {"quote": "", "confidence": "low"}."""

USER_PROMPT = "Transcribe the quote of this t-shirt design. JSON only."

REFINE_PROMPT = """Transcribe the quote of this t-shirt design. JSON only.
A faster model read it as (it may be wrong - check every word against the image):
{reads}"""


@dataclass
class QuoteResult:
    quote: str = ""
    confidence: str = ""          # high | medium | low ("" = not read)
    model: str = ""
    level: str = ""               # "" | easy | hard
    error: str = ""
    fatal: bool = False           # key / credit problem: no point asking another model


def api_key() -> str | None:
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    return None if key in _PLACEHOLDER_KEYS else key


def available() -> tuple[bool, str]:
    if not config.OCR_ENABLED:
        return False, "OCR tắt (OCR_ENABLED=0)"
    if api_key() is None:
        return False, "chưa có ANTHROPIC_API_KEY trong .env"
    return True, f"{config.CLAUDE_MODEL}" + (f" -> {config.CLAUDE_HARD_MODEL}" if config.CLAUDE_HARD_MODEL else "")


def _encode(src: Path, tone: str) -> tuple[str, str]:
    im = artwork.readable_copy(src, tone)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=92)
    return "image/jpeg", base64.standard_b64encode(buf.getvalue()).decode("ascii")


_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse(text: str) -> tuple[str, str]:
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip(), flags=re.MULTILINE)
    m = _OBJ_RE.search(raw)
    try:
        data = json.loads(m.group(0) if m else raw)
    except (json.JSONDecodeError, AttributeError):
        return _clean(raw.strip('"')), "low"
    if not isinstance(data, dict):
        return "", "low"
    quote = _clean(str(data.get("quote") or ""))
    conf = str(data.get("confidence") or "low").strip().lower()
    if conf not in ("high", "medium", "low") or not quote:
        conf = "low" if not quote else (conf if conf in ("high", "medium") else "low")
    return quote, conf


def _clean(s: str) -> str:
    s = s.translate(str.maketrans("’‘“”", "''\"\""))
    return "\n".join(re.sub(r"[^\S\n]+", " ", line).strip()
                     for line in s.replace("\r\n", "\n").split("\n") if line.strip())


def ask(model: str, media: str, data: str, user: str, hard: bool) -> QuoteResult:
    import anthropic

    key = api_key()
    if key is None:
        return QuoteResult(error="no ANTHROPIC_API_KEY", fatal=True)
    client = anthropic.Anthropic(api_key=key, max_retries=2,
                                 timeout=config.CLAUDE_HARD_TIMEOUT_S if hard else config.CLAUDE_TIMEOUT_S)
    params = dict(
        model=model,
        max_tokens=config.CLAUDE_HARD_MAX_TOKENS if hard else config.CLAUDE_MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}},
            {"type": "text", "text": user},
        ]}],
    )
    try:
        if hard:
            extra = {}
            if config.CLAUDE_HARD_EFFORT:
                extra["output_config"] = {"effort": config.CLAUDE_HARD_EFFORT}
            if config.CLAUDE_REFUSAL_FALLBACK:
                extra.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
                resp = client.beta.messages.create(**params, **extra)
            else:
                resp = client.messages.create(**params, **extra)
        else:
            resp = client.messages.create(**params)
    except anthropic.AuthenticationError:
        return QuoteResult(error="ANTHROPIC_API_KEY không hợp lệ", fatal=True)
    except anthropic.PermissionDeniedError as e:
        return QuoteResult(error=f"key không có quyền: {e.message}", fatal=True)
    except anthropic.NotFoundError as e:
        return QuoteResult(error=f"model {model!r} không dùng được: {e.message}")
    except anthropic.BadRequestError as e:
        if "credit balance" in str(e.message).lower():
            return QuoteResult(error="tài khoản Anthropic hết credit", fatal=True)
        return QuoteResult(error=f"bad request: {e.message}")
    except anthropic.RateLimitError:
        return QuoteResult(error="rate limited")
    except anthropic.APIStatusError as e:
        return QuoteResult(error=f"API error {e.status_code}: {e.message}")
    except anthropic.APIConnectionError as e:
        return QuoteResult(error=f"lỗi mạng: {e.__class__.__name__}")
    text = "".join(b.text for b in resp.content if b.type == "text")
    if resp.stop_reason == "refusal":
        return QuoteResult(error="model từ chối", model=model)
    if not text.strip():
        return QuoteResult(error=f"không có câu trả lời ({resp.stop_reason})", model=model)
    quote, conf = parse(text)
    return QuoteResult(quote=quote, confidence=conf, model=getattr(resp, "model", model) or model)


def read_quote(src: Path, tone: str) -> QuoteResult:
    ok, why = available()
    if not ok:
        return QuoteResult(error=why)
    media, data = _encode(Path(src), tone)
    first = ask(config.CLAUDE_MODEL, media, data, USER_PROMPT, hard=False)
    first.level = "easy"
    if first.fatal or not config.CLAUDE_HARD_MODEL:
        return first
    if not first.error and first.confidence == "high" and first.quote:
        return first
    hint = json.dumps({"quote": first.quote, "confidence": first.confidence}, ensure_ascii=False) \
        if not first.error else "(no read - the first model failed)"
    second = ask(config.CLAUDE_HARD_MODEL, media, data, REFINE_PROMPT.format(reads=hint), hard=True)
    second.level = "hard"
    if second.error:
        if first.error:
            return second
        first.confidence = "low"      # keep the cheap read, but ask for a manual check
        first.error = ""
        return first
    return second
