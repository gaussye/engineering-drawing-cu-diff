"""Generate fixed demo credentials locally; never print or package passwords."""

import argparse
import json
import os
from pathlib import Path
import re
import secrets

from werkzeug.security import generate_password_hash


def generate(directory, username="demo"):
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", username):
        raise ValueError("Invalid demo username")
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    password = secrets.token_urlsafe(24)
    settings = {"username": username,
                "password_hash": generate_password_hash(password, method="scrypt:32768:8:1", salt_length=16)}
    for name, content in (("settings.json", settings),
                          ("credentials.json", {"username": username, "password": password})):
        descriptor = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(content, stream, indent=2)
            stream.write("\n")
    return directory / "credentials.json"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path("local") / "demo-login")
    parser.add_argument("--username", default="demo")
    args = parser.parse_args()
    path = generate(args.directory, args.username)
    print(f"Private credential handoff: {path}. Deploy settings.json only; never upload credentials.json.")
