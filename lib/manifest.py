"""output/<batch>/manifest.csv - one row per campaign to create.

status: "pending" | "done" | "error: <reason>". group_id / image_id / url are filled by
the upload. The file is rewritten atomically after every change.
"""
from __future__ import annotations

import csv
import os
import threading
from pathlib import Path

import config

COLUMNS = ["filename", "slug", "title", "quote", "source", "tone", "flags",
           "origin_id", "origin_name", "status", "group_id", "image_id", "url", "note", "clone_state",
           "default_base_product_id"]
_lock = threading.Lock()


def path_for(batch: str) -> Path:
    return config.OUTPUT_DIR / batch / "manifest.csv"


def read(batch: str) -> list[dict]:
    p = path_for(batch)
    if not p.exists():
        return []
    with p.open(encoding="utf-8-sig", newline="") as f:
        rows = [dict(r) for r in csv.DictReader(f)]
    for r in rows:
        for c in COLUMNS:
            r.setdefault(c, "")
            if r[c] is None:
                r[c] = ""
    return rows


def write(batch: str, rows: list[dict]) -> None:
    p = path_for(batch)
    p.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        tmp = p.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({c: r.get(c, "") for c in COLUMNS})
        for attempt in range(5):
            try:
                os.replace(tmp, p)
                break
            except PermissionError:      # the CSV is open in Excel: retry, then give up loudly
                if attempt == 4:
                    raise
                threading.Event().wait(0.5)


def batches() -> list[str]:
    if not config.OUTPUT_DIR.exists():
        return []
    names = [d.name for d in config.OUTPUT_DIR.iterdir() if (d / "manifest.csv").exists()]
    return sorted(names, reverse=True)


def is_done(row: dict) -> bool:
    return (row.get("status") or "").strip().lower() == "done"


def is_error(row: dict) -> bool:
    return (row.get("status") or "").strip().lower().startswith("error")
