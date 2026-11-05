"""Offline self test: canvas, slugs, titles, review -> export, upload flow against a fake
Mayzing (no network, nothing is created). Run: .venv\\Scripts\\python.exe tools\\selftest.py"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="mz_selftest_"))
# every data folder into the temp dir BEFORE the modules that read them are imported
config.INPUT_DIR = TMP / "input"
config.DONE_DIR = config.INPUT_DIR / "done"
config.OUTPUT_DIR = TMP / "output"
config.WORK_DIR = TMP / "work"
config.LOG_DIR = TMP / "logs"
config.AUTH_DIR = TMP / "auth"
config.SETTINGS_FILE = TMP / "settings.json"
config.USED_SLUGS_FILE = TMP / "used_slugs.txt"
config.OCR_ENABLED = False
config.SLUG_CHECK_MAYZING = False
config.CLONE_POLL_S = 0
config.TITLE_CASE = "upper"
config.TITLE_MAX_LENGTH = 60
config.TITLE_MAX_WORDS = 8
config.ensure_dirs()

from PIL import Image, ImageDraw  # noqa: E402

from lib import artwork, manifest, mayzing, review, slug, titles  # noqa: E402
import upload_batch  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, what: str) -> None:
    print(("ok   " if cond else "FAIL ") + what)
    if not cond:
        FAILS.append(what)


def make_design(path: Path, size, colour, opaque=False) -> None:
    im = Image.new("RGBA", size, (255, 255, 255, 255) if opaque else (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    w, h = size
    d.rectangle([w * 0.1, h * 0.1, w * 0.9, h * 0.5], fill=colour)
    im.save(path)


# ---------------------------------------------------------------- slugs / titles
check(slug.from_filename("ge123 front") == "ge123", "slug from 'ge123 front'")
check(slug.from_filename("SK123_2") == "sk123", "slug from 'SK123_2'")
check(slug.from_filename("216") == "216", "slug from '216'")
check(slug.from_filename("12") == "", "no slug from '12'")
check(slug.from_filename("I like dogs") == "", "no slug from words")
check(slug.slugify("ICH KAM ICH SAH") == "ich-kam-ich-sah", "slugify title")
check(slug.slugify("Größe & Mut") == "groesse-and-mut", "slugify umlauts")
check(slug.clean("https://luvsmilegoods.com/ab123/") == "ab123", "clean pasted URL")
check(not slug.is_valid("checkout"), "blocked slug")
check(slug.unique("ge123", {"ge123", "ge123a"}) == "ge123b", "code suffix letters")
check(slug.unique("ich-kam", {"ich-kam"}) == "ich-kam-2", "word suffix -2")
t, f = titles.build_title("ich kam\nich sah")
check(t == "ICH KAM ICH SAH" and not f, "title one line upper")
t, f = titles.build_title("")
check(t == "" and "NO_TITLE" in f, "empty title flag")
t, f = titles.build_title("x " * 300)
check(len(t) <= config.TITLE_MAX_LENGTH and len(t.split()) <= config.TITLE_MAX_WORDS
      and "TITLE_SHORTENED" in f, "title shortened to configured word/character limits")
quote = "WENN DU DICH MIT DER GROSSEN SCHWESTER ANLEGST GIBT ES IMMER EINE JÜNGERE SCHWESTER"
t, f = titles.build_title(quote)
check(t == "WENN DU DICH MIT DER GROSSEN SCHWESTER ANLEGST" and "TITLE_SHORTENED" in f,
      "long quote uses the first eight complete words")
long_words = ["SCHWESTERCHEN"] * 8
t, f = titles.build_title(" ".join(long_words))
check(len(t) <= config.TITLE_MAX_LENGTH and all(w == long_words[0] for w in t.split()),
      "character limit preserves complete words")

# ---------------------------------------------------------------- canvas
src = TMP / "a.png"
make_design(src, (1000, 800), (20, 20, 20, 255))
a = artwork.prepare(src, TMP / "a_out.png", TMP / "a_thumb.png")
with Image.open(TMP / "a_out.png") as out:
    check(out.size == config.ARTWORK_CANVAS_SIZE, f"canvas size {out.size}")
    check(round(out.info.get("dpi", (0, 0))[0]) == 300, "300 dpi")
check(a.tone == "dark", f"dark tone ({a.tone})")
check(any(x.startswith("UPSCALE") or x.startswith("TOO_SMALL") for x in a.flags), f"enlarge flag {a.flags}")
make_design(src, config.ARTWORK_CANVAS_SIZE, (240, 240, 240, 255))
a = artwork.prepare(src, TMP / "b_out.png", TMP / "b_thumb.png")
check("PRINT_READY" in a.flags and a.tone == "light", f"print ready light {a.flags} {a.tone}")
make_design(src, (900, 900), (200, 30, 30, 255), opaque=True)
a = artwork.prepare(src, TMP / "c_out.png", TMP / "c_thumb.png")
check("OPAQUE" in a.flags, "opaque flag")

# ---------------------------------------------------------------- review -> export
config.save_settings({"store_slug": "TEST", "store_domain": "shop.test", "origin_dark": "g-dark",
                      "origin_light": "g-light", "origin_default": "g-dark", "auto_publish": True,
                      "extra_tags": ["extra tag"]})
make_design(config.INPUT_DIR / "ge123 front.png", (2000, 2000), (10, 10, 10, 255))
make_design(config.INPUT_DIR / "Funny cat.png", (2000, 2000), (250, 250, 250, 255))
make_design(config.INPUT_DIR / "ge124.png", (2000, 2000), (250, 250, 250, 255))
slug.commit(["ge124"])                      # already used by an earlier batch
rv = review.Review()
n = rv.run_prep(lambda m: None, use_ocr=False)
check(n == 3, f"prep made {n} cards")
items = {i["source"]: i for i in rv.snapshot()}
check(items["ge123 front.png"]["slug"] == "ge123", "card slug ge123")
check(items["ge124.png"]["slug"] == "ge124a", f"used slug gets letter ({items['ge124.png']['slug']})")
check("NO_SLUG" in items["Funny cat.png"]["flags"], "words file: no slug until a title exists")
try:
    rv.update(items["Funny cat.png"]["uid"], {"decision": "approved"})
    check(False, "approve blocked without title")
except ValueError:
    check(True, "approve blocked without title")
it = rv.update(items["Funny cat.png"]["uid"], {"quote": "Funny cat\nsays hi"})
check(it["title"] == "FUNNY CAT SAYS HI" and it["slug"] == "funny-cat-says-hi", f"title + slug from quote ({it['slug']})")
rv.update(items["ge123 front.png"]["uid"], {"quote": "ich kam ich sah"})
rv.update(items["ge124.png"]["uid"], {"quote": "ich kam ich sah"})     # same title, other design
check(rv.approve_all_ok() == 3, "approve all ok cards")
batch, count = rv.export({"g-dark": "Dark origin", "g-light": "Light origin"}, say=lambda m: None)
rows = manifest.read(batch)
check(count == 3 and len(rows) == 3, "export 3 rows")
check(all((config.OUTPUT_DIR / batch / r["filename"]).exists() for r in rows), "export files")
by_slug = {r["slug"]: r for r in rows}
check(by_slug["ge123"]["origin_id"] == "g-dark" and by_slug["ge124a"]["origin_id"] == "g-light",
      "origin by tone")
check((config.DONE_DIR / batch / "ge123 front.png").exists(), "input moved to done")
check("ge123" in slug.load_used(), "slug committed")
check(not rv.snapshot(), "review emptied after export")


# ---------------------------------------------------------------- upload against a fake Mayzing
class FakeMZ:
    """Behaves like what the dashboard showed on 2026-11-02: clone names a campaign after
    its image; image name = uploaded file name without extension."""
    def __init__(self, say=None):
        self.groups_ = [
            {"id": "g-dark", "name": "Dark origin", "slug": "dark-origin", "clonable": True, "processing": False, "tags": ["t1"]},
            {"id": "g-light", "name": "Light origin", "slug": "light-origin", "clonable": True, "processing": False, "tags": []},
            {"id": "g-old", "name": "OLD", "slug": "funny-cat-says-hi", "clonable": True, "processing": False, "tags": []},
        ]
        self.images, self.clones, self.updates, self.published = {}, [], [], 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def stores(self):
        return [{"slug": "TEST", "name": "Test store", "domainName": "shop.test"}]

    def groups(self, store):
        return [dict(g) for g in self.groups_]

    def group(self, store, gid):
        return next((dict(g) for g in self.groups_ if g["id"] == gid), None)

    def slug_free(self, store, s):
        return all(g["slug"] != s for g in self.groups_)

    def upload_image(self, path, name):
        iid = f"img{len(self.images) + 1}"
        self.images[iid] = mayzing._safe_filename(name)
        return {"id": iid, "name": self.images[iid], "width": 4200, "height": 4800}

    def clone_group(self, gid, image_ids):
        self.clones.append((gid, list(image_ids)))
        for iid in image_ids:
            n = len(self.groups_)
            self.groups_.append({"id": f"new{n}", "name": self.images[iid], "slug": f"auto-{n}",
                                 "clonable": True, "processing": True, "tags": ["t1"]})
        return [{"id": f"CC-{len(self.clones)}", "status": "PENDING"}]

    def wait_task(self, tid, on_progress=None, timeout_s=None):
        t = {"id": tid, "status": "SUCCEEDED", "finishedProcessingAt": "x",
             "summary": {"progress": 1, "succeeded": 1, "failed": 0}}
        if on_progress:
            on_progress(t)
        for g in self.groups_:
            g["processing"] = False
        return t

    def update_group(self, store, gid, name, slug_, tags):
        g = next(g for g in self.groups_ if g["id"] == gid)
        g.update(name=name, slug=slug_, tags=tags)
        self.updates.append((gid, name, slug_, tuple(tags)))
        return dict(g)

    def publish(self, store):
        self.published += 1
        return {"slug": store}


fake = FakeMZ()
real = mayzing.Mayzing
mayzing.Mayzing = lambda say=None, interactive=False: fake
try:
    s = upload_batch.run(batch, say=lambda m: None)
finally:
    mayzing.Mayzing = real
rows = {r["slug"]: r for r in manifest.read(batch)}
check(s["done"] == 2 and s["errors"] == 1, f"upload: 2 done, 1 error ({s})")
check("đã thuộc campaign khác" in rows["funny-cat-says-hi"]["status"], "taken URL -> error, nothing cloned")
check(rows["ge123"]["status"] == "done" and rows["ge123"]["url"] == "https://shop.test/ge123", "ge123 done + url")
check(len(fake.clones) == 2, f"two clone tasks: one per origin ({len(fake.clones)})")
check(all("extra tag" in u[3] and "t1" in u[3] for u in fake.updates), "tags = cloned + extra")
check(fake.published == 1, "published once")

# same title twice for ONE origin -> two separate clone tasks (names must be unique per task)
r1 = {"title": "SAME"}
r2 = {"title": "same"}
r3 = {"title": "OTHER"}
ch = upload_batch._chunks([r1, r2, r3], 50)
check(len(ch) == 2 and r2 in ch[1], "duplicate titles split into separate tasks")
m = upload_batch.match_new_groups([{"title": "A"}, {"title": "B"}],
                                  [{"id": "2", "name": "B"}, {"id": "1", "name": "A (1)"}])
check(m[0]["id"] == "1" and m[1]["id"] == "2", "match by name / prefix")

# retry: the error row gets a new URL and runs again
rows_l = manifest.read(batch)
for r in rows_l:
    if r["slug"] == "funny-cat-says-hi":
        r["slug"], r["status"] = "funny-cat-2", "pending"
manifest.write(batch, rows_l)
mayzing.Mayzing = lambda say=None, interactive=False: fake
try:
    s2 = upload_batch.run(batch, say=lambda m: None, publish=False)
finally:
    mayzing.Mayzing = real
check(s2["done"] == 1 and fake.published == 1, f"retry creates the last one, no publish ({s2})")

check(mayzing._is_unauthenticated([{"message": "Unauthorized", "extensions": {"code": "UNAUTHENTICATED"}}]),
      "detect UNAUTHENTICATED")
check(mayzing._safe_filename('A "B"/C') == "A  B  C", "safe multipart file name")

shutil.rmtree(TMP, ignore_errors=True)
print()
print("SELFTEST " + ("OK" if not FAILS else f"FAILED: {len(FAILS)}"))
sys.exit(1 if FAILS else 0)
