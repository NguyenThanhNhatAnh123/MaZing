"""All tunable settings of mayzing-bulk-upload.

Values here are defaults; .env overrides the secrets / model ids and
settings.json (written by the dashboard) holds the shop choices: store,
origin campaigns, extra tags, auto publish.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
load_dotenv(ENV_FILE)

# ---------------------------------------------------------------------------
# folders
# ---------------------------------------------------------------------------
INPUT_DIR = ROOT / "input"              # design files dropped here
DONE_DIR = INPUT_DIR / "done"           # design files of exported batches
OUTPUT_DIR = ROOT / "output"            # output/<batch>/<slug>.png + manifest.csv
WORK_DIR = ROOT / "work"                # review state + prepared images (not shared)
LOG_DIR = ROOT / "logs"
AUTH_DIR = ROOT / "auth"
CHROME_PROFILE_DIR = AUTH_DIR / "chrome-profile"   # Mayzing login lives here (never shared)
SETTINGS_FILE = ROOT / "settings.json"
USED_SLUGS_FILE = ROOT / "used_slugs.txt"          # every exported URL, never reused

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")

# ---------------------------------------------------------------------------
# Mayzing (verified 2026-11-02 on dashboard.mayzing.com, see CLAUDE.md)
# ---------------------------------------------------------------------------
MAYZING_DASHBOARD = "https://dashboard.mayzing.com"
MAYZING_GRAPHQL = MAYZING_DASHBOARD + "/api/graphql"
MAYZING_LOGIN_HOST = "auth.mayzing.com"      # Auth0 login page (/u/login)
BROWSER_EXECUTABLE_PATH = os.environ.get("BROWSER_EXECUTABLE_PATH", "").strip()  # normal Chrome launcher
LOGIN_TIMEOUT_S = 600            # how long the login window waits for the user
GRAPHQL_TIMEOUT_MS = 120_000
UPLOAD_TIMEOUT_MS = 300_000      # one artwork upload (multipart)
CLONE_MAX_PER_TASK = 50          # Mayzing: "up to 50" (help) / modal says 100
CLONE_TASK_TIMEOUT_S = 900       # one Bulk clone task
CLONE_POLL_S = 4
GROUP_READY_TIMEOUT_S = 300      # new campaigns must stop "processing" before the rename
MAX_UPLOAD_BYTES = 50 * 1024 * 1024   # Mayzing: max file size 50 MB
STORE_CACHE_S = 300              # campaign list cache in the dashboard

# ---------------------------------------------------------------------------
# print canvas (Mayzing recommends 4200 x 4800 px @ 300 dpi)
# ---------------------------------------------------------------------------
ARTWORK_MODE = os.environ.get("ARTWORK_MODE", "canvas")   # "canvas" | "original"
ARTWORK_CANVAS_SIZE = (4200, 4800)
ARTWORK_WIDTH_FRACTION = 0.97    # design width on the canvas
ARTWORK_TOP_FRACTION = 0.015     # gap above the design
ARTWORK_BOTTOM_FRACTION = 0.015
ARTWORK_TRIM_ALPHA = 8           # pixels at least this opaque count as design
OUTPUT_DPI = 300
ENLARGE_WARN_FACTOR = 1.3        # amber UPSCALE from here
ENLARGE_BAD_FACTOR = 4.0         # red TOO_SMALL from here
MIN_TRANSPARENT_SHARE = 0.02     # less transparent than this = opaque file (OPAQUE flag)
THUMB_SIZE = 700                 # preview long side in the dashboard

# design tone: alpha-weighted mean luminance of the visible pixels (0..255)
DESIGN_TONE_DARK_MAX = 110       # <= dark design (needs a light shirt)
DESIGN_TONE_LIGHT_MIN = 150      # >= light design (needs a dark shirt)

# ---------------------------------------------------------------------------
# quote reading (Claude vision). No key -> titles are typed on the review card.
# ---------------------------------------------------------------------------
OCR_ENABLED = os.environ.get("OCR_ENABLED", "1") not in ("0", "false", "no")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")        # reads every design
CLAUDE_HARD_MODEL = os.environ.get("CLAUDE_HARD_MODEL", "claude-opus-5-5")  # unsure reads; "" = off
CLAUDE_HARD_EFFORT = "high"
CLAUDE_MAX_TOKENS = 4000
CLAUDE_HARD_MAX_TOKENS = 16000
CLAUDE_TIMEOUT_S = 120
CLAUDE_HARD_TIMEOUT_S = 300
CLAUDE_REFUSAL_FALLBACK = True   # server-side fallback on the strong model
VISION_MAX_SIDE = 2000           # image side sent to the model
OCR_MIXED_BACKGROUND = (128, 128, 128)
OCR_WORKERS = 4                  # designs read in parallel

# ---------------------------------------------------------------------------
# titles (Mayzing campaign name: max 255 characters)
# ---------------------------------------------------------------------------
TITLE_CASE = os.environ.get("TITLE_CASE", "upper")   # "upper" | "as_printed"
TITLE_MAX_LENGTH = max(1, min(255, int(os.environ.get("TITLE_MAX_LENGTH", "60"))))
TITLE_MAX_WORDS = max(1, int(os.environ.get("TITLE_MAX_WORDS", "8")))
FORBIDDEN_WORDS: list[str] = []   # removed from titles, e.g. ["Official", "Merch"]

# ---------------------------------------------------------------------------
# URL slugs
# ---------------------------------------------------------------------------
SLUG_FROM_TITLE = True           # file name without a code -> slug made from the title
SLUG_CHECK_MAYZING = True        # checked only after the user enables the paired Chrome session
BLOCKED_SLUGS = ("faq", "product", "search", "store", "about-us", "checkout", "copyright",
                 "orders", "payment", "privacy", "terms", "thankyou")   # Mayzing's own list

# ---------------------------------------------------------------------------
# dashboard
# ---------------------------------------------------------------------------
DASHBOARD_HOST = "127.0.0.1"
DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "8780"))
MOVE_EXPORTED_INPUT = True       # input/<file> -> input/done/<batch>/ after export
DELETE_UPLOADED_FILES = False    # remove output PNG of a row once its campaign exists

# ---------------------------------------------------------------------------
# settings.json (store, origins, tags) - written by the dashboard
# ---------------------------------------------------------------------------
DEFAULT_SETTINGS = {
    "store_slug": "",        # Mayzing store id, e.g. "3E0J"
    "store_name": "",
    "store_domain": "",      # e.g. "luvsmilegoods.com" (campaign links)
    "origin_default": "",    # campaign (group) id used when no tone origin applies
    "origin_dark": "",       # origin for DARK designs (light shirt)
    "origin_light": "",      # origin for LIGHT designs (dark shirt)
    "extra_tags": [],        # added to every new campaign
    "auto_publish": True,    # "Publish changes" after an upload run
    "default_products": {},  # origin group id -> preferred base-product id for new campaigns
    "ocr": True,
}
_settings_lock = threading.Lock()


def load_settings() -> dict:
    data = dict(DEFAULT_SETTINGS)
    try:
        raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            data.update({k: v for k, v in raw.items() if k in DEFAULT_SETTINGS})
    except (OSError, ValueError):
        pass
    return data


def save_settings(changes: dict) -> dict:
    with _settings_lock:
        data = load_settings()
        data.update({k: v for k, v in changes.items() if k in DEFAULT_SETTINGS})
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, SETTINGS_FILE)
        return data


def ensure_dirs() -> None:
    for d in (INPUT_DIR, DONE_DIR, OUTPUT_DIR, WORK_DIR, LOG_DIR, AUTH_DIR):
        d.mkdir(parents=True, exist_ok=True)
