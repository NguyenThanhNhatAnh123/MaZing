"""Run the same offline checks on CI and on a prepared release."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    for path in [ROOT / "tools" / "selftest.py", *sorted((ROOT / "tools").glob("test_*.py"))]:
        subprocess.run([sys.executable, str(path)], cwd=ROOT, check=True)
    for path in (ROOT / "chrome-extension").glob("*.js"):
        subprocess.run(["node", "--check", str(path)], cwd=ROOT, check=True)
    for path in sorted((ROOT / "tools").glob("test_*.js")):
        subprocess.run(["node", str(path)], cwd=ROOT, check=True)
    # Parse every inline script, including the connection guide, without a DOM.
    for path in (ROOT / "lib").glob("*.html"):
        for script in re.findall(r"<script\b[^>]*>(.*?)</script>", path.read_text(encoding="utf-8"), re.S | re.I):
            if script.strip():
                subprocess.run(["node", "--check"], input=script, text=True, encoding="utf-8", cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
