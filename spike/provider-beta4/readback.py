#!/usr/bin/env python3
"""Did provider 1.0.12-beta.4 actually WRITE the ADR-0003 fields?

Terraform reporting success proves nothing — that is the whole ADR-0003 finding:
the provider accepts these fields, reports success, treats them as computed, and
never writes them. Only what SCM returns is evidence.

    python3 spike/provider-beta4/readback.py            # TEST 1: are they written?
    python3 spike/provider-beta4/readback.py --expect-cleared   # TEST 2
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from fwgitops.scmapi import ScmApiError, ScmCredentials, ScmSession  # noqa: E402

INTENDED = {
    "application": ["web-browsing"],
    "log_setting": "Cortex Data Lake",
    "profile_setting": {"group": ["best-practice"]},
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--folder", default="GitOps")
    ap.add_argument("--name", default="fwgitops-beta4-probe")
    ap.add_argument("--expect-cleared", action="store_true",
                    help="TEST 2: after re-applying WITHOUT the fields, are they gone?")
    args = ap.parse_args()

    try:
        session = ScmSession(credentials=ScmCredentials.from_env())
        payload = session.request(
            "GET", "/config/security/v1/security-rules",
            params={"folder": args.folder, "limit": 200},
        )
    except ScmApiError as e:
        print(f"GET security-rules failed: {e}", file=sys.stderr)
        return 1

    match = next((r for r in payload.get("data", []) if r.get("name") == args.name), None)
    if match is None:
        print(f"probe rule {args.name!r} NOT FOUND in {args.folder!r}", file=sys.stderr)
        return 1

    print("=== WHAT SCM ACTUALLY STORED ===")
    print(json.dumps({k: v for k, v in match.items() if k not in ("id", "tfid")},
                     indent=2, sort_keys=True))

    got = {k: match.get(k) for k in INTENDED}

    if args.expect_cleared:
        print("\n=== TEST 2: does OMITTING the field clear it? ===")
        still_set = {k: v for k, v in got.items() if v not in (None, [], {}, ["any"])}
        for k, v in got.items():
            print(f"  {k}: {v!r}")
        print()
        if still_set:
            print("RESULT: omission did NOT clear these — the value SURVIVED:")
            print(f"        {sorted(still_set)}")
            print("=> `computed` here means 'provider re-reads and keeps'. A plan showing")
            print("   `-> (known after apply)` is benign for these fields.")
            return 0
        print("RESULT: omission CLEARED the fields.")
        print("=> `computed` does NOT mean preserved. A plan showing")
        print("   `-> (known after apply)` is a SILENT DATA LOSS warning, not noise.")
        return 3

    print("\n=== TEST 1: does beta.4 write what ADR-0003 says is dropped? ===")
    dropped = []
    for key, want in INTENDED.items():
        ok = got[key] == want
        print(f"  {'WRITTEN ' if ok else 'DROPPED '} {key}: wanted {want!r}, got {got[key]!r}")
        if not ok:
            dropped.append(key)

    print()
    if dropped:
        print(f"RESULT: 1.0.12-beta.4 STILL DROPS {dropped}.")
        print("=> ADR-0003 holds; `fwgitops enrich` is still required. Pin stays 1.0.11.")
        return 3
    print("RESULT: 1.0.12-beta.4 WRITES all three.")
    print("=> ADR-0003 can be revisited: enrich could retire for these fields and the")
    print("   compiler own them directly. Re-probe ordering separately before doing so.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
