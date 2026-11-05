"""Provision the shared dashboard account without storing a plaintext password."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

from dotenv import dotenv_values
from werkzeug.security import generate_password_hash

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-podsach-env", type=Path,
                        help="Copy the existing PodSach username/password into a private password hash.")
    args = parser.parse_args()
    if args.from_podsach_env:
        values = dotenv_values(args.from_podsach_env)
        username = values.get("PODSACH_WEB_USER") or ""
        password = values.get("PODSACH_WEB_PASSWORD") or ""
    else:
        username = input("Username: ").strip()
        password = getpass.getpass("Password: ")
        if password != getpass.getpass("Confirm password: "):
            parser.error("Passwords do not match.")
    if not username or username != username.strip() or len(username) > 254 or not password:
        parser.error("Configure a username and password before enabling dashboard login.")
    destination = config.BASE_DATA_ROOT / "auth" / "web-login.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump({"username": username, "password_hash": generate_password_hash(password)}, handle)
        handle.write("\n")
    temporary.chmod(0o600)
    os.replace(temporary, destination)
    print("Dashboard account configured. Existing login sessions are invalidated.")


if __name__ == "__main__":
    main()
