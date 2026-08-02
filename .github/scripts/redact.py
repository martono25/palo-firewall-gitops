#!/usr/bin/env python3
"""Strip credential values out of files before they are PUBLISHED.

GitHub masks secrets in the live log stream. It does NOT mask them in uploaded
artifact contents or in `gh pr comment` bodies — and this workflow publishes
both, from files that capture terraform's stderr while SCM_CLIENT_SECRET sits in
the job env. A provider or auth error echoing a credential would land in a
durable artifact and a public PR comment while the visible log looked clean.

Not observed. Structural, and cheap to close.

Literal substring replacement, not regex: a secret can contain any character, and
building a pattern out of one risks both mis-escaping and putting the value into
a command line where it could be logged.

Usage:  redact.py FILE [FILE ...]      (values come from the environment)
"""
from __future__ import annotations

import os
import sys

#: Every env var whose VALUE must never appear in a published file.
SECRET_VARS = (
    "SCM_CLIENT_SECRET",
    "SCM_CLIENT_ID",
    "SCM_SCOPE",
    "GH_TOKEN",
    "GITHUB_TOKEN",
)

#: Shorter than this and a "secret" is probably a placeholder or empty; replacing
#: it would corrupt unrelated text without protecting anything.
MIN_LEN = 8


def redact(text: str, secrets: list[str]) -> tuple[str, int]:
    hits = 0
    for value in secrets:
        if value in text:
            hits += text.count(value)
            text = text.replace(value, "***REDACTED***")
    return text, hits


def main(paths: list[str]) -> int:
    secrets = [
        v for v in (os.environ.get(name, "") for name in SECRET_VARS)
        if v and len(v) >= MIN_LEN
    ]
    if not secrets:
        print("redact: no secret values in the environment — nothing to do")
        return 0

    total = 0
    for path in paths:
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8", errors="replace") as fh:
            original = fh.read()
        cleaned, hits = redact(original, secrets)
        if hits:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(cleaned)
            print(f"redact: {path} — {hits} occurrence(s) removed")
            total += hits
    print(f"redact: {total} occurrence(s) removed across {len(paths)} file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
