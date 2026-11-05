"""Review cards: prep (analyse + read the quote + URL), edits on the card, export of a batch.

State lives in work/review.json + work/img/<uid>.png (print file) + work/thumb/<uid>.png,
so the dashboard can be closed and opened again without losing the cards.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Callable

import config
from lib import artwork, manifest, ocr, slug as slugs, titles

STATE_FILE = config.WORK_DIR / "review.json"
IMG_DIR = config.WORK_DIR / "img"
THUMB_DIR = config.WORK_DIR / "thumb"

# flags that block "Duyệt" (red) / that only warn (amber); anything else is info
RED_FLAGS = ("NO_DESIGN", "NO_TITLE", "NO_SLUG", "OCR_ERROR", "OPAQUE", "TOO_SMALL", "FILE_TOO_BIG",
             "LOW_CONFIDENCE", "SLUG_INVALID")
BLOCKING = ("NO_DESIGN", "NO_TITLE", "NO_SLUG", "FILE_TOO_BIG", "SLUG_INVALID")


def flag_level(flag: str) -> str:
    name = flag.split(":", 1)[0]
    if name in RED_FLAGS:
        return "bad"
    if name in ("UPSCALE", "SLUG_SUFFIX", "TITLE_SHORTENED", "FORBIDDEN_REMOVED", "SLUG_UNCHECKED",
                "SLUG_FROM_TITLE", "MEDIUM_CONFIDENCE"):
        return "warn"
    return "info"


class Review:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.items: list[dict] = []
        self._load()

    # -- persistence ------------------------------------------------------------
    def _load(self) -> None:
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            self.items = [i for i in data.get("items", []) if isinstance(i, dict)]
        except (OSError, ValueError):
            self.items = []

    def save(self) -> None:
        with self.lock:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps({"items": self.items}, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, STATE_FILE)

    def get(self, uid: str) -> dict | None:
        with self.lock:
            return next((i for i in self.items if i["uid"] == uid), None)

    def snapshot(self) -> list[dict]:
        with self.lock:
            return [dict(i) for i in self.items]

    # -- slugs ------------------------------------------------------------------
    def _taken_local(self, except_uid: str = "") -> set[str]:
        taken = slugs.load_used()
        with self.lock:
            taken |= {i["slug"].lower() for i in self.items if i.get("slug") and i["uid"] != except_uid}
        return taken

    @staticmethod
    def _remote_checker(mz, store_slug: str):
        if mz is None or not store_slug:
            return None
        cache: dict[str, bool] = {}

        def taken(s: str) -> bool | None:
            if s not in cache:
                try:
                    cache[s] = not mz.slug_free(store_slug, s)
                except Exception:  # noqa: BLE001 - fail-soft
                    return None
            return cache[s]
        return taken

    def _apply_slug(self, item: dict, wanted: str, mz=None, store_slug: str = "") -> None:
        item["flags"] = [f for f in item.get("flags", [])
                         if f.split(":")[0] not in ("NO_SLUG", "SLUG_SUFFIX", "SLUG_INVALID",
                                                     "SLUG_FROM_TITLE", "SLUG_UNCHECKED")]
        from_title = False
        if not wanted and config.SLUG_FROM_TITLE and item.get("title"):
            wanted = slugs.slugify(item["title"])
            from_title = bool(wanted)
        if not wanted:
            item["slug"] = ""
            item["flags"].append("NO_SLUG")
            return
        if not slugs.is_valid(wanted):
            item["slug"] = wanted
            item["flags"].append("SLUG_INVALID")
            return
        checker = self._remote_checker(mz, store_slug)
        got = slugs.unique(wanted, self._taken_local(item["uid"]), checker)
        item["slug"] = got
        item["slug_wanted"] = wanted
        if from_title:
            item["flags"].append("SLUG_FROM_TITLE")
        if got != wanted:
            item["flags"].append(f"SLUG_SUFFIX:{wanted}")
        if checker is None and config.SLUG_CHECK_MAYZING:
            item["flags"].append("SLUG_UNCHECKED")

    # -- quote / title ------------------------------------------------------------
    @staticmethod
    def _apply_quote(item: dict, quote: str) -> None:
        item["quote"] = quote
        title, tflags = titles.build_title(quote)
        item["title"] = title
        keep = [f for f in item.get("flags", [])
                if f.split(":")[0] not in ("NO_TITLE", "TITLE_SHORTENED", "FORBIDDEN_REMOVED")]
        item["flags"] = keep + tflags

    # -- prep ---------------------------------------------------------------------
    def pending_inputs(self) -> list[Path]:
        known = {i["source"] for i in self.snapshot()}
        if not config.INPUT_DIR.exists():
            return []
        files = [p for p in sorted(config.INPUT_DIR.iterdir(), key=lambda p: p.name.lower())
                 if p.is_file() and p.suffix.lower() in config.IMAGE_EXTS and p.name not in known]
        return files

    def run_prep(self, say: Callable[[str], None], use_ocr: bool = True, limit: int = 0,
                 progress: Callable[[int, int], None] | None = None, origin: str = "") -> int:
        """origin: campaign (group id) for every new card ("" = by design tone)."""
        files = self.pending_inputs()
        if limit:
            files = files[:limit]
        if not files:
            say("Không có file design mới trong input\\.")
            return 0
        settings = config.load_settings()
        ocr_ok, ocr_why = ocr.available()
        if not use_ocr:
            ocr_note = "tắt"
        elif not ocr_ok:
            ocr_note = f"không dùng được - {ocr_why}"
        else:
            ocr_note = ocr_why
        use_ocr = use_ocr and ocr_ok
        say(f"Prep {len(files)} file. Đọc quote: {ocr_note}")

        # 1) analyse + print file (sequential: large images)
        new_items = []
        for n, path in enumerate(files, 1):
            uid = uuid.uuid4().hex[:10]
            try:
                a = artwork.prepare(path, IMG_DIR / f"{uid}.png", THUMB_DIR / f"{uid}.png")
            except Exception as e:  # noqa: BLE001
                say(f"  {path.name}: không đọc được file ({e.__class__.__name__}: {e})")
                continue
            item = {"uid": uid, "source": path.name, "width": a.width, "height": a.height,
                    "tone": a.tone, "luminance": round(a.luminance, 1), "enlarge": round(a.enlarge, 2),
                    "flags": list(a.flags), "quote": "", "title": "", "slug": "", "slug_wanted": "",
                    "ocr_conf": "", "ocr_model": "", "ocr_level": "", "ocr_error": "",
                    "origin": origin or "", "decision": "", "created": time.time()}
            new_items.append((item, path))
            say(f"  [{n}/{len(files)}] {path.name}: {a.width}x{a.height}, tông {a.tone}"
                + (f", {', '.join(a.flags)}" if a.flags else ""))
            if progress:
                progress(n, len(files) * (2 if use_ocr else 1))

        # 2) quote (parallel)
        if use_ocr and new_items:
            say(f"Đọc quote {len(new_items)} design bằng Claude...")
            done = 0
            with ThreadPoolExecutor(max_workers=config.OCR_WORKERS) as ex:
                futs = {ex.submit(ocr.read_quote, path, item["tone"]): item for item, path in new_items}
                for fut in futs:
                    item = futs[fut]
                    try:
                        q = fut.result()
                    except Exception as e:  # noqa: BLE001
                        q = ocr.QuoteResult(error=f"{e.__class__.__name__}: {e}")
                    done += 1
                    item["ocr_model"], item["ocr_level"] = q.model, q.level
                    if q.error:
                        item["ocr_error"] = q.error
                        item["flags"].append("OCR_ERROR")
                        say(f"  {item['source']}: lỗi đọc quote - {q.error}")
                    else:
                        item["ocr_conf"] = q.confidence
                        if q.confidence == "low":
                            item["flags"].append("LOW_CONFIDENCE")
                        elif q.confidence == "medium":
                            item["flags"].append("MEDIUM_CONFIDENCE")
                        say(f"  {item['source']}: \"{q.quote.replace(chr(10), ' / ')}\" [{q.confidence} · {q.model}]")
                    self._apply_quote(item, q.quote)
                    if progress:
                        progress(len(new_items) + done, len(new_items) * 2)
        else:
            for item, _ in new_items:
                self._apply_quote(item, "")

        # 3) URL: file name code, else from the title; unique locally and on the store
        mz_ctx = None
        store = settings.get("store_slug") or ""
        if config.SLUG_CHECK_MAYZING and store:
            try:
                from lib import mayzing
                if mayzing.has_session_file():
                    mz_ctx = mayzing.Mayzing(say=say).__enter__()
                    mz_ctx.stores()           # one quick check: no waiting per slug when it fails
            except Exception as e:  # noqa: BLE001
                say(f"Không kiểm tra được URL trên Mayzing ({e}); URL sẽ được kiểm tra lại lúc upload.")
                mz_ctx = None
        try:
            for item, path in new_items:
                with self.lock:
                    self.items.append(item)
                self._apply_slug(item, slugs.from_filename(path.stem), mz_ctx, store)
        finally:
            if mz_ctx is not None:
                mz_ctx.__exit__(None, None, None)
        self.save()
        say(f"Xong prep: {len(new_items)} thẻ mới. Mở tab Duyệt để kiểm tra.")
        return len(new_items)

    # -- card edits -----------------------------------------------------------------
    def update(self, uid: str, changes: dict, mz=None, store_slug: str = "") -> dict:
        """Apply card edits. mz (an open lib.mayzing.Mayzing) lets a typed URL be checked
        on the store."""
        with self.lock:
            item = self.get(uid)
            if item is None:
                raise KeyError(uid)
            follow_title = "SLUG_FROM_TITLE" in item.get("flags", []) or not item.get("slug")
            if "quote" in changes:
                self._apply_quote(item, str(changes["quote"] or "").strip())
            if "title" in changes:
                t, tflags = titles.build_title(str(changes["title"] or ""))
                item["title"] = t
                item["flags"] = [f for f in item["flags"] if f.split(":")[0] not in
                                 ("NO_TITLE", "TITLE_SHORTENED", "FORBIDDEN_REMOVED")] + tflags
            if "slug" in changes:
                wanted = slugs.clean(str(changes["slug"] or ""))
                self._apply_slug(item, wanted, mz, store_slug)
            elif follow_title and ("quote" in changes or "title" in changes):
                self._apply_slug(item, "", mz, store_slug)     # URL made from the title follows it
            if "origin" in changes:
                item["origin"] = str(changes["origin"] or "").strip()
            if "decision" in changes:
                d = changes["decision"]
                if d == "approved" and any(f.split(":")[0] in BLOCKING for f in item["flags"]):
                    raise ValueError("Thẻ còn lỗi đỏ (thiếu tiêu đề / URL / design) - sửa trước khi duyệt.")
                item["decision"] = d if d in ("approved", "rejected") else ""
            if "LOW_CONFIDENCE" in item["flags"] and ("quote" in changes or "title" in changes):
                item["flags"].remove("LOW_CONFIDENCE")      # a person has read it now
            if "MEDIUM_CONFIDENCE" in item["flags"] and ("quote" in changes or "title" in changes):
                item["flags"].remove("MEDIUM_CONFIDENCE")
            if "OCR_ERROR" in item["flags"] and ("quote" in changes or "title" in changes) and item["title"]:
                item["flags"].remove("OCR_ERROR")
            self.save()
            return dict(item)

    def remove(self, uid: str, delete_file: bool = False) -> None:
        with self.lock:
            item = self.get(uid)
            if item is None:
                return
            self.items.remove(item)
            for d in (IMG_DIR, THUMB_DIR):
                (d / f"{uid}.png").unlink(missing_ok=True)
            if delete_file:
                (config.INPUT_DIR / item["source"]).unlink(missing_ok=True)
            self.save()

    def discard(self) -> int:
        """Drop every card (the design files stay in input/)."""
        with self.lock:
            n = len(self.items)
            for item in self.items:
                for d in (IMG_DIR, THUMB_DIR):
                    (d / f"{item['uid']}.png").unlink(missing_ok=True)
            self.items = []
            self.save()
        return n

    def approve_all_ok(self) -> int:
        n = 0
        with self.lock:
            for item in self.items:
                if not item.get("decision") and not any(flag_level(f) == "bad" for f in item["flags"]):
                    item["decision"] = "approved"
                    n += 1
            self.save()
        return n

    # -- export ---------------------------------------------------------------------
    @staticmethod
    def resolve_origin(item: dict, settings: dict) -> str:
        if item.get("origin"):
            return item["origin"]
        by_tone = {"dark": settings.get("origin_dark"), "light": settings.get("origin_light")}
        return by_tone.get(item.get("tone")) or settings.get("origin_default") or \
            settings.get("origin_dark") or settings.get("origin_light") or ""

    def export(self, origin_names: dict[str, str] | None = None,
               say: Callable[[str], None] = print) -> tuple[str, int]:
        """Write approved cards to output/<batch>/ + manifest.csv. Returns (batch, count)."""
        settings = config.load_settings()
        origin_names = origin_names or {}
        with self.lock:
            chosen = [i for i in self.items if i.get("decision") == "approved"]
            if not chosen:
                raise ValueError("Chưa có thẻ nào được duyệt.")
            for i in chosen:
                if any(f.split(":")[0] in BLOCKING for f in i["flags"]):
                    raise ValueError(f"{i['source']}: còn lỗi đỏ, không xuất được.")
                if not self.resolve_origin(i, settings):
                    raise ValueError("Chưa chọn campaign gốc (origin): vào tab Mayzing chọn origin, "
                                     "hoặc chọn origin trên thẻ.")
            seen: set[str] = set()
            for i in chosen:
                if i["slug"].lower() in seen:
                    raise ValueError(f"URL {i['slug']} trùng giữa 2 thẻ.")
                seen.add(i["slug"].lower())
            used = slugs.load_used()
            clash = [i["slug"] for i in chosen if i["slug"].lower() in used]
            if clash:
                raise ValueError(f"URL đã dùng ở batch trước: {', '.join(clash)} - sửa URL trên thẻ.")

            batch = datetime.now().strftime("%Y-%m-%d_%H%M")
            n = 1
            while (config.OUTPUT_DIR / batch).exists():
                n += 1
                batch = datetime.now().strftime("%Y-%m-%d_%H%M") + f"_{n}"
            out_dir = config.OUTPUT_DIR / batch
            out_dir.mkdir(parents=True)
            done_dir = config.DONE_DIR / batch
            rows = []
            for i in chosen:
                fname = f"{i['slug']}.png"
                shutil.copyfile(IMG_DIR / f"{i['uid']}.png", out_dir / fname)
                origin = self.resolve_origin(i, settings)
                rows.append({
                    "filename": fname, "slug": i["slug"], "title": i["title"], "quote": i["quote"],
                    "source": i["source"], "tone": i["tone"],
                    "flags": " ".join(f for f in i["flags"] if flag_level(f) != "info"),
                    "origin_id": origin, "origin_name": origin_names.get(origin, ""),
                    "status": "pending", "group_id": "", "image_id": "", "url": "", "note": "",
                })
            manifest.write(batch, rows)
            slugs.commit([i["slug"] for i in chosen])
            for i in chosen:
                src = config.INPUT_DIR / i["source"]
                if config.MOVE_EXPORTED_INPUT and src.exists():
                    done_dir.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(src), str(done_dir / i["source"]))
                self.items.remove(i)
                for d in (IMG_DIR, THUMB_DIR):
                    (d / f"{i['uid']}.png").unlink(missing_ok=True)
            self.save()
        say(f"Đã xuất batch {batch}: {len(rows)} campaign -> output\\{batch}\\")
        return batch, len(rows)
