#!/usr/bin/env python3
"""Read the device-scope probe back from SCM — and check it stayed put.

Terraform reporting success proves nothing (scm_security_rule accepts fields,
reports success, and never writes them). Only what SCM returns is evidence.

Two questions, not one:
  1. FIDELITY  — did the provider write the fields at device scope?
  2. ISOLATION — is the object visible ONLY under the target device? If it also
     appears under the other firewall or in ngfw-shared, then `device=` is not
     an isolating scope and cannot be the "one firewall" target.

Run from the repo root after `terraform apply`:
    python3 spike/device-scope-probe/readback.py
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from fwgitops.scmapi import ScmApiError, ScmCredentials, ScmSession  # noqa: E402

TARGET = "007955000893662"   # disconnected
OTHER = "007955000894453"    # connected — must NOT see the probe
PARENT = "ngfw-shared"       # shared folder — must NOT see the probe

INTENDED = {
    "comment": "fwgitops device-scope probe — safe to delete",
    "layer3.mtu": 1500,
    "layer3.ip": [{"name": "10.99.98.1/30"}],
}


def _fetch(session, **scope):
    params = dict(scope)
    params["limit"] = 200
    return session.request(
        "GET", "/config/network/v1/ethernet-interfaces", params=params
    ).get("data", [])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="ethernet1/5")
    args = ap.parse_args()

    try:
        session = ScmSession(credentials=ScmCredentials.from_env())
        on_target = _fetch(session, device=TARGET)
        on_other = _fetch(session, device=OTHER)
        in_parent = _fetch(session, folder=PARENT)
    except ScmApiError as e:
        print(f"readback failed: {e}", file=sys.stderr)
        return 1

    match = next((i for i in on_target if i.get("name") == args.name), None)
    if match is None:
        print(f"probe {args.name!r} NOT FOUND under device={TARGET}", file=sys.stderr)
        print("If `terraform apply` reported success, that absence IS the finding.",
              file=sys.stderr)
        return 1

    print("=== WHAT SCM ACTUALLY STORED (device scope) ===")
    print(json.dumps(match, indent=2, sort_keys=True))

    layer3 = match.get("layer3") or {}
    got = {
        "comment": match.get("comment"),
        "layer3.mtu": layer3.get("mtu"),
        "layer3.ip": layer3.get("ip"),
    }

    print("\n=== 1. FIDELITY ===")
    dropped = []
    for key, want in INTENDED.items():
        ok = got[key] == want
        print(f"  {'HONORED ' if ok else 'DROPPED '} {key}: wanted {want!r}, got {got[key]!r}")
        if not ok:
            dropped.append(key)

    print("\n=== 2. ISOLATION ===")
    leaked_other = [i for i in on_other if i.get("name") == args.name]
    leaked_parent = [i for i in in_parent if i.get("name") == args.name]
    print(f"  device={TARGET} (target)  : present  <- expected")
    print(f"  device={OTHER} (other)    : {'PRESENT — LEAKED' if leaked_other else 'absent   <- expected'}")
    print(f"  folder={PARENT} (shared)  : {'PRESENT — LEAKED' if leaked_parent else 'absent   <- expected'}")
    print(f"  scope fields on the object: folder={match.get('folder')!r} device={match.get('device')!r}")

    leaked = bool(leaked_other or leaked_parent)

    print()
    if leaked:
        print("RESULT: `device=` is NOT an isolating scope — the object is visible")
        print("        outside the target device.")
        print("=> It cannot serve as the 'one firewall' target. Re-scope before building.")
        return 3
    if dropped:
        print(f"RESULT: device scope isolates, but the provider DROPPED {dropped}.")
        print("=> Device-scoped writes would need an enrich-style REST path.")
        return 3
    print("RESULT: `device=` is an isolating scope AND the provider writes faithfully.")
    print("=> Safe to build `device:` targeting on. Still open: whether writing an")
    print("   EXISTING inherited interface at device scope overrides or mutates the")
    print("   shared object — deliberately not probed here.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
