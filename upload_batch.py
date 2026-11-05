"""Create the campaigns of a batch on Mayzing with "Bulk clone".

For every origin campaign of the batch (manifest column origin_id), up to
CLONE_MAX_PER_TASK rows at a time:
  1. upload each print file to the Artwork library, the image named after the campaign
     title (Bulk clone names each new campaign after its artwork),
  2. CloneGroup(origin, image ids) = the "Bulk clone" button; wait for the task,
  3. find the new campaigns (ids that were not there before, matched by name),
  4. UpdateStoreGroup: name = title, URL = slug, tags = cloned tags + extra tags,
then "Publish changes" once (PublishCatalog) unless --no-publish.

Usage:
  python upload_batch.py --batch 2026-11-02_0930            (asks before creating)
  python upload_batch.py --batch 2026-11-02_0930 --yes
  python upload_batch.py --batch ... --dry-run             checks only, creates nothing
  python upload_batch.py --batch ... --retry-errors --limit 2 --no-publish
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Callable

import config
from lib import manifest, mayzing, product_defaults
from lib.context import WORKSPACE_ID

log = logging.getLogger("upload")


def setup_logging(batch: str) -> logging.Handler:
    config.ensure_dirs()
    handler = logging.FileHandler(config.LOG_DIR / f"{batch}.log", encoding="utf-8")
    workspace = WORKSPACE_ID.get()
    handler.addFilter(lambda record: WORKSPACE_ID.get() == workspace)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    return handler


def _norm(s: str) -> str:
    return " ".join((s or "").split()).casefold()


def _chunks(rows: list[dict], size: int) -> list[list[dict]]:
    """Split into clone tasks of <= size rows whose image names are unique inside the task
    (two equal titles in one task could not be told apart afterwards)."""
    out: list[list[dict]] = []
    pending = list(rows)
    while pending:
        cur, names, rest = [], set(), []
        for r in pending:
            n = _norm(r["title"])
            if len(cur) < size and n not in names:
                cur.append(r)
                names.add(n)
            else:
                rest.append(r)
        out.append(cur)
        pending = rest
    return out


def match_new_groups(rows: list[dict], new_groups: list[dict]) -> dict[int, dict]:
    """{row index in `rows`: group} by name (= the uploaded image name). A single leftover
    row + a single leftover group are paired as well."""
    result: dict[int, dict] = {}
    free = list(new_groups)
    for idx, r in enumerate(rows):
        want = _norm(r.get("_image_name") or r["title"])
        g = next((g for g in free if _norm(g.get("name")) == want), None)
        if g is None:
            g = next((g for g in free if _norm(g.get("name")).startswith(want)), None)
        if g is not None:
            result[idx] = g
            free.remove(g)
    left = [i for i in range(len(rows)) if i not in result]
    if len(left) == 1 and len(free) == 1:
        result[left[0]] = free[0]
    return result


def run(batch: str, *, retry_errors: bool = False, limit: int = 0, publish: bool | None = None,
        dry_run: bool = False, say: Callable[[str], None] | None = None,
        confirm: Callable[[str], bool] | None = None) -> dict:
    handler = setup_logging(batch)

    def tell(msg: str, level: int = logging.INFO) -> None:
        log.log(level, msg)
        if say:
            say(msg)

    try:
        return _run(batch, retry_errors, limit, publish, dry_run, tell, confirm)
    finally:
        log.removeHandler(handler)
        handler.close()


def _run(batch, retry_errors, limit, publish, dry_run, tell, confirm) -> dict:
    settings = config.load_settings()
    store = settings.get("store_slug") or ""
    if publish is None:
        publish = bool(settings.get("auto_publish"))
    extra_tags = [t for t in (settings.get("extra_tags") or []) if t]
    summary = {"batch": batch, "done": 0, "errors": 0, "skipped": 0, "published": False}
    if not store:
        raise mayzing.MayzingError("Chưa chọn store Mayzing (tab Mayzing).")
    rows = manifest.read(batch)
    if not rows:
        raise mayzing.MayzingError(f"Batch {batch} không có manifest.csv")
    out_dir = config.OUTPUT_DIR / batch

    def save() -> None:
        manifest.write(batch, rows)

    def fail(r: dict, reason: str) -> None:
        r["status"] = f"error: {reason}"
        summary["errors"] += 1
        tell(f"  {r['slug']}: LỖI - {reason}", logging.WARNING)
        save()

    todo = [r for r in rows if not manifest.is_done(r)
            and (retry_errors or not manifest.is_error(r))]
    if limit:
        todo = todo[:limit]
    if not todo:
        tell("Không còn dòng nào cần upload (dùng --retry-errors để chạy lại dòng lỗi).")
        return summary
    tell(f"Batch {batch}: {len(todo)} campaign cần tạo trên store {settings.get('store_name') or store}.")

    with mayzing.Mayzing(say=tell, interactive=True) as mz:
        stores = {s["slug"]: s for s in mz.stores()}
        if store not in stores:
            raise mayzing.MayzingError(f"Tài khoản đang đăng nhập không có store {store}.")
        domain = stores[store].get("domainName") or settings.get("store_domain") or ""
        groups = mz.groups(store)
        by_id = {g["id"]: g for g in groups}
        by_slug = {(g.get("slug") or "").lower(): g for g in groups}

        # ---- checks (read-only) ------------------------------------------------
        ready: list[dict] = []
        detailed_origins: dict[str, dict] = {}
        for r in todo:
            r["status"] = "pending"
            # A submitted clone is never submitted a second time. Recover its group
            # from the saved snapshot, or require a Task log check if ambiguous.
            if r.get("clone_state") and not r.get("group_id"):
                try:
                    state = json.loads(r["clone_state"])
                    before = set(state["before_ids"])
                    wanted = _norm(state["image_name"])
                    matches = [g for g in groups if g["id"] not in before and _norm(g.get("name")) == wanted]
                except (ValueError, KeyError, TypeError):
                    matches = []
                if len(matches) != 1:
                    fail(r, "Bulk clone đã được gửi nhưng chưa xác định được campaign; kiểm tra Task log. Tool không tạo lại.")
                    continue
                r["group_id"] = matches[0]["id"]
                save()
            if r.get("group_id") and r["group_id"] not in by_id:
                fail(r, "campaign đã tạo không còn trong store; kiểm tra trên Mayzing. Tool không tạo lại.")
                continue
            path = out_dir / r["filename"]
            if not r.get("group_id") and not path.exists():
                fail(r, f"không thấy file {r['filename']}")
                continue
            if not r.get("group_id") and path.stat().st_size > config.MAX_UPLOAD_BYTES:
                fail(r, "file lớn hơn 50 MB")
                continue
            if not r.get("title"):
                fail(r, "thiếu tiêu đề")
                continue
            origin = by_id.get(r.get("origin_id")) or by_slug.get((r.get("origin_id") or "").lower())
            if origin is None:
                fail(r, f"không thấy campaign gốc {r.get('origin_id') or '(trống)'} trên store")
                continue
            if not r.get("group_id") and not origin.get("clonable", True):
                fail(r, f"campaign gốc {origin['slug']} không cho Bulk clone")
                continue
            r["origin_id"], r["origin_name"] = origin["id"], origin.get("name") or r.get("origin_name", "")
            if not r.get('group_id') and not r.get('clone_state'):
                preference = (settings.get('default_products') or {}).get(origin['id']) or {}
                r['default_base_product_id'] = preference.get('base_product_id', '') if isinstance(preference, dict) else ''
            if r.get('default_base_product_id') and not r.get('group_id'):
                if origin['id'] not in detailed_origins:
                    detailed_origins[origin['id']] = mz.group_products(store, origin['id']) or {}
                detailed_origin = detailed_origins[origin['id']]
                try:
                    product_defaults.resolve(detailed_origin, r['default_base_product_id'])
                except mayzing.MayzingError as exc:
                    fail(r, str(exc))
                    continue
            existing = by_slug.get(r["slug"].lower())
            if existing is not None:
                if _norm(existing.get("name")) == _norm(r["title"]) and r.get('default_base_product_id'):
                    r['group_id'] = existing['id']
                elif _norm(existing.get("name")) == _norm(r["title"]):
                    r.update(status="done", group_id=existing["id"], url=mayzing.campaign_url(domain, existing["slug"]),
                             note="đã có trên Mayzing (lần chạy trước)")
                    summary["skipped"] += 1
                    tell(f"  {r['slug']}: đã có trên Mayzing, bỏ qua.")
                    save()
                    continue
                elif existing["id"] != r.get("group_id"):
                    fail(r, f"URL /{r['slug']} đã thuộc campaign khác ({existing.get('name')}) - đổi URL rồi chạy lại")
                    continue
            try:
                if existing is None and not mz.slug_free(store, r["slug"]):
                    fail(r, f"URL /{r['slug']} đã được dùng trên store - đổi URL rồi chạy lại")
                    continue
            except mayzing.SessionExpired:
                raise
            except mayzing.MayzingError as e:
                tell(f"  {r['slug']}: không kiểm tra được URL ({e}), vẫn tiếp tục", logging.WARNING)
            ready.append(r)
        save()
        if not ready:
            tell("Không có dòng nào sẵn sàng.")
            return summary

        plan: dict[str, list[dict]] = {}
        for r in ready:
            plan.setdefault(r["origin_id"], []).append(r)
        for oid, rs in plan.items():
            tell(f"  origin {by_id[oid].get('name')} (/{by_id[oid].get('slug')}): {len(rs)} campaign")
        if dry_run:
            tell("--dry-run: chỉ kiểm tra, không tạo campaign.")
            return summary
        if confirm is not None and not confirm(f"Tạo {len(ready)} campaign trên Mayzing?"):
            tell("Đã huỷ.")
            return summary

        # ---- create ----------------------------------------------------------------
        for oid, rs in plan.items():
            origin = by_id[oid]
            for r in rs:
                if r.get("group_id"):
                    tell(f"  {r['slug']}: tiếp tục campaign đã tạo, không clone lại.")
                    _finish_group(mz, store, domain, r, by_id[r["group_id"]], extra_tags, tell, fail, save, summary)
            for chunk in _chunks([r for r in rs if not r.get("group_id")], config.CLONE_MAX_PER_TASK):
                _clone_chunk(mz, store, domain, origin, chunk, extra_tags, out_dir, tell, fail, save, summary)

        if publish and summary["done"]:
            try:
                mz.publish(store)
                summary["published"] = True
                tell("Đã bấm \"Publish changes\" - Mayzing đang đưa campaign mới lên store (xem Task log).")
            except mayzing.MayzingError as e:
                tell(f"Publish lỗi: {e} - bấm \"Publish changes\" trên Mayzing.", logging.WARNING)
        elif summary["done"]:
            tell("Chưa publish: bấm \"Publish changes\" (dashboard hoặc Mayzing) để campaign lên store.")

    if config.DELETE_UPLOADED_FILES:
        for r in rows:
            if manifest.is_done(r):
                (out_dir / r["filename"]).unlink(missing_ok=True)
    tell(f"Xong: {summary['done']} tạo mới, {summary['skipped']} đã có, {summary['errors']} lỗi.")
    return summary


def _clone_chunk(mz, store, domain, origin, chunk, extra_tags, out_dir, tell, fail, save, summary) -> None:
    # 1) artworks
    uploaded: list[dict] = []
    seen_images: set[str] = set()
    for r in chunk:
        try:
            img = mz.upload_image(out_dir / r["filename"], r["title"])
        except mayzing.SessionExpired:
            raise
        except mayzing.MayzingError as e:
            fail(r, f"upload artwork: {e}")
            continue
        if img["id"] in seen_images:
            fail(r, "file design trùng với dòng khác (Mayzing trả về cùng một ảnh)")
            continue
        seen_images.add(img["id"])
        r["image_id"] = img["id"]
        r["_image_name"] = img.get("name") or r["title"]
        uploaded.append(r)
        tell(f"  {r['slug']}: đã tải artwork ({img.get('width')}x{img.get('height')})")
        save()
    if not uploaded:
        return

    # 2) Bulk clone
    before = {g["id"] for g in mz.groups(store)}
    for r in uploaded:
        r["clone_state"] = json.dumps({"before_ids": sorted(before), "image_name": r["_image_name"]}, ensure_ascii=False)
    save()  # Persist before sending: even an uncertain response must not cause a duplicate.
    tell(f"Bulk clone {len(uploaded)} artwork từ /{origin.get('slug')}...")
    try:
        tasks = mz.clone_group(origin["id"], [r["image_id"] for r in uploaded])
        for r in uploaded:
            state = json.loads(r["clone_state"])
            state["task_ids"] = [t["id"] for t in tasks]
            r["clone_state"] = json.dumps(state, ensure_ascii=False)
        save()
        last_progress = {}

        def progress(t: dict) -> None:
            p = ((t.get("summary") or {}).get("progress"))
            if isinstance(p, (int, float)) and p != last_progress.get(t.get("id")):
                last_progress[t.get("id")] = p
                tell(f"  task {t.get('id')}: {t.get('status')} {round(p * 100)}%")
        for task in tasks:
            task = mz.wait_task(task["id"], on_progress=progress)
            s = task.get("summary") or {}
            tell(f"  task {task.get('id')}: {task.get('status')} (thành công {s.get('succeeded')}, lỗi {s.get('failed')})")
    except mayzing.SessionExpired:
        raise
    except mayzing.MayzingError as e:
        for r in uploaded:
            fail(r, f"Bulk clone: {e}")
        return

    # 3) new campaigns
    new_groups: list[dict] = []
    deadline = time.time() + 60
    while True:
        new_groups = [g for g in mz.groups(store) if g["id"] not in before]
        if len(new_groups) >= len(uploaded) or time.time() > deadline:
            break
        time.sleep(config.CLONE_POLL_S)
    matched = match_new_groups(uploaded, new_groups)
    if len(new_groups) and len(matched) < len(uploaded):
        tell("  campaign mới: " + ", ".join(f"{g.get('name')} (/{g.get('slug')})" for g in new_groups))

    # 4) rename + URL
    for idx, r in enumerate(uploaded):
        g = matched.get(idx)
        if g is None:
            fail(r, "Bulk clone xong nhưng không tìm thấy campaign mới của artwork này - kiểm tra trên Mayzing")
            continue
        r["group_id"] = g["id"]
        save()
        _finish_group(mz, store, domain, r, g, extra_tags, tell, fail, save, summary)
    for r in uploaded:
        r.pop("_image_name", None)


def _finish_group(mz, store, domain, r, g, extra_tags, tell, fail, save, summary) -> None:
    try:
        g = _wait_ready(mz, store, g)
        tags = list(dict.fromkeys([*(g.get("tags") or []), *extra_tags]))
        res = mz.update_group(store, g["id"], r["title"], r["slug"], tags)
        if r.get('default_base_product_id'):
            detailed = mz.group_products(store, g['id']) or {}
            product_id = product_defaults.resolve(detailed, r['default_base_product_id'])
            mz.set_first_product(store, detailed, product_id)
            tell(f"  {r['slug']}: đã đặt sản phẩm mở đầu theo lựa chọn của origin.")
    except mayzing.SessionExpired:
        raise
    except mayzing.MayzingError as e:
        fail(r, f"đổi tên / URL (campaign /{g.get('slug')} đã được tạo): {e}")
        return
    if (res.get("slug") or "").lower() != r["slug"].lower():
        fail(r, f"Mayzing giữ URL /{res.get('slug')} thay vì /{r['slug']}")
        return
    r.update(status="done", url=mayzing.campaign_url(domain, res["slug"]), note="")
    r.pop("_image_name", None)
    summary["done"] += 1
    tell(f"  {r['slug']}: XONG - {r['title']}" + (f" -> {r['url']}" if r["url"] else ""))
    save()


def _wait_ready(mz, store: str, g: dict) -> dict:
    deadline = time.time() + config.GROUP_READY_TIMEOUT_S
    while g.get("processing") and time.time() < deadline:
        time.sleep(config.CLONE_POLL_S)
        g = mz.group(store, g["id"]) or g
    if g.get("processing"):
        raise mayzing.MayzingError("campaign còn đang xử lý; thử tiếp tục sau, tool sẽ không clone lại")
    return g


def main(argv: list[str] | None = None, say: Callable[[str], None] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Bulk clone a batch to Mayzing")
    ap.add_argument("--batch", help="output/<batch> (default: newest)")
    ap.add_argument("--retry-errors", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--yes", action="store_true", help="do not ask before creating")
    ap.add_argument("--dry-run", action="store_true", help="checks only")
    pub = ap.add_mutually_exclusive_group()
    pub.add_argument("--publish", dest="publish", action="store_true", default=None)
    pub.add_argument("--no-publish", dest="publish", action="store_false")
    args = ap.parse_args(argv)

    batch = args.batch or next(iter(manifest.batches()), None)
    if not batch:
        print("Chưa có batch nào trong output\\")
        return 1
    batch = Path(batch).name
    out = say or (lambda m: print(m, flush=True))

    def ask(q: str) -> bool:
        if args.yes:
            return True
        try:
            return input(q + " [y/N] ").strip().lower() in ("y", "yes", "c", "co", "có")
        except EOFError:
            return False

    try:
        s = run(batch, retry_errors=args.retry_errors, limit=args.limit, publish=args.publish,
                dry_run=args.dry_run, say=out, confirm=None if say else ask)
    except mayzing.SessionExpired as e:
        out(str(e))
        return 2
    except mayzing.MayzingError as e:
        out(f"Lỗi: {e}")
        return 1
    finally:
        if say is None:
            mayzing.shutdown()         # CLI: close the tool's Chrome window
    return 0 if not s["errors"] else 3


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(errors="replace")
    except AttributeError:
        pass
    sys.exit(main())
