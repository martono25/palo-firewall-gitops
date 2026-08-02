#!/usr/bin/env python3
"""T5 verdict — read the probe zone back from SCM and diff against intent.

This is the whole experiment. Terraform reporting success proves nothing: on
scm_security_rule the provider ACCEPTS profile_setting / log_setting / ordering,
reports success, treats them as computed, and never writes them (ADR-0003,
verified live 2026-07-28). Only what SCM returns counts as evidence.

Run from the repo root, after `terraform apply`:
    python3 <path>/readback.py --folder <FOLDER> [--zone fwgitops-probe-zone]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from fwgitops.scmapi import ScmApiError, ScmCredentials, ScmSession  # noqa: E402

# What main.tf asked for. Keep in sync with the resource block.
INTENDED_TOP = {
    "enable_user_identification": True,
    "enable_device_identification": True,
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--folder", required=True)
    ap.add_argument("--zone", default="fwgitops-probe-zone")
    ap.add_argument(
        "--expect-network",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="phase-2 expectation, e.g. --expect-network zone_protection_profile=default",
    )
    args = ap.parse_args()

    try:
        session = ScmSession(credentials=ScmCredentials.from_env())
        payload = session.request(
            "GET", "/config/network/v1/zones", params={"folder": args.folder, "limit": 200}
        )
    except ScmApiError as e:
        print(f"GET zones failed: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"request failed: {e}", file=sys.stderr)
        return 2

    items = payload.get("data", [])
    match = next((z for z in items if z.get("name") == args.zone), None)
    if match is None:
        print(f"probe zone {args.zone!r} NOT FOUND in folder {args.folder!r}", file=sys.stderr)
        print(
            "If `terraform apply` reported success, that absence is itself the finding.",
            file=sys.stderr,
        )
        return 1

    print("=== WHAT SCM ACTUALLY STORED ===")
    print(json.dumps(match, indent=2, sort_keys=True))

    dropped, honored = [], []
    for key, want in INTENDED_TOP.items():
        got = match.get(key)
        target = honored if got == want else dropped
        target.append(f"{key}: wanted {want!r}, got {got!r}")

    net = match.get("network") or {}
    if not isinstance(net, dict):
        net = {}
    for spec in args.expect_network:
        key, _, want = spec.partition("=")
        got = net.get(key)
        target = honored if str(got) == want else dropped
        target.append(f"network.{key}: wanted {want!r}, got {got!r}")

    print("\n=== VERDICT ===")
    for line in honored:
        print(f"  HONORED  {line}")
    for line in dropped:
        print(f"  DROPPED  {line}")

    print()
    if dropped:
        print("RESULT: the provider does NOT faithfully write all probed zone fields.")
        print("=> Zone support needs an enrich-style REST subsystem, as rules did.")
        print("=> A3 cost rises materially. Revisit the deferred scope in TODOS.md.")
        return 3
    print("RESULT: the provider writes the probed zone fields faithfully.")
    print("=> A3 is a dataclass + loader + tfvars mapping. No enrich needed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
