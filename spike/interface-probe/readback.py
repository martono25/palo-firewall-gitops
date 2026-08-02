#!/usr/bin/env python3
"""ADR-0005 prerequisite 4 — read the probe interface back from SCM.

Terraform reporting success proves nothing. On scm_security_rule the provider
ACCEPTS the fields, reports success, treats them as computed, and never writes
them. Only what SCM returns is evidence.

Run from the repo root after `terraform apply`:
    python3 spike/interface-probe/readback.py --folder GitOps
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from fwgitops.scmapi import ScmApiError, ScmCredentials, ScmSession  # noqa: E402

INTENDED = {
    "comment": "fwgitops fidelity probe — safe to delete",
    "layer3.mtu": 1500,
    "layer3.ip": [{"name": "10.99.99.1/30"}],
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--folder", default="GitOps")
    ap.add_argument("--name", default="$eth-fwgitops-probe")
    args = ap.parse_args()

    try:
        session = ScmSession(credentials=ScmCredentials.from_env())
        payload = session.request(
            "GET", "/config/network/v1/ethernet-interfaces",
            params={"folder": args.folder, "limit": 200},
        )
    except ScmApiError as e:
        print(f"GET ethernet-interfaces failed: {e}", file=sys.stderr)
        return 1

    match = next((i for i in payload.get("data", []) if i.get("name") == args.name), None)
    if match is None:
        print(f"probe interface {args.name!r} NOT FOUND in {args.folder!r}", file=sys.stderr)
        print("If `terraform apply` reported success, that absence IS the finding.",
              file=sys.stderr)
        return 1

    print("=== WHAT SCM ACTUALLY STORED ===")
    print(json.dumps(match, indent=2, sort_keys=True))

    layer3 = match.get("layer3") or {}
    got = {
        "comment": match.get("comment"),
        "layer3.mtu": layer3.get("mtu"),
        "layer3.ip": layer3.get("ip"),
    }

    print("\n=== VERDICT ===")
    dropped = []
    for key, want in INTENDED.items():
        ok = got[key] == want
        print(f"  {'HONORED ' if ok else 'DROPPED '} {key}: wanted {want!r}, got {got[key]!r}")
        if not ok:
            dropped.append(key)

    print()
    if dropped:
        print("RESULT: the provider does NOT faithfully write scm_ethernet_interface.")
        print("=> InterfaceRequest needs an enrich-style REST subsystem, as rules do.")
        print("=> Re-scope ADR-0005 accordingly before building.")
        return 3
    print("RESULT: the provider writes scm_ethernet_interface faithfully.")
    print("=> InterfaceRequest is a compiler + tfvars mapping. No enrich needed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
