#!/usr/bin/env python3
"""TODOS P1 gate — read the probe router back from SCM.

Terraform reporting success proves nothing. On scm_security_rule the provider
ACCEPTS the fields, reports success, treats them as computed, and never writes
them. Only what SCM returns is evidence.

Run from the repo root after `terraform apply`:
    python3 spike/router-probe/readback.py --folder GitOps
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from fwgitops.scmapi import ScmApiError, ScmCredentials, ScmSession  # noqa: E402

PROBE_IFACE = "$eth-fwgitops-probe"

#: What main.tf asked for, keyed by the dotted path the compiler emits. Checked
#: individually so a PARTIAL write is reported as partial — "the routes landed
#: but admin_dist did not" is a different remedy from "nothing landed".
INTENDED = {
    "vrf.interface": [PROBE_IFACE],
    "route[probe-via-ip].destination": "192.0.2.0/24",
    "route[probe-via-ip].nexthop.ip_address": "10.99.99.2",
    "route[probe-via-ip].metric": 17,
    "route[probe-via-ip].admin_dist": 33,
    "route[probe-via-interface].destination": "198.51.100.0/24",
    "route[probe-via-interface].interface": PROBE_IFACE,
}


def _routes_of(vrf: dict) -> dict:
    table = ((vrf.get("routing_table") or {}).get("ip") or {}).get("static_route") or []
    return {r.get("name"): r for r in table if isinstance(r, dict)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--folder", default="GitOps")
    ap.add_argument("--name", default="fwgitops-probe-router")
    args = ap.parse_args()

    try:
        session = ScmSession(credentials=ScmCredentials.from_env())
        payload = session.request(
            "GET", "/config/network/v1/logical-routers",
            params={"folder": args.folder, "limit": 200},
        )
    except ScmApiError as e:
        print(f"GET logical-routers failed: {e}", file=sys.stderr)
        return 1

    match = next((r for r in payload.get("data", []) if r.get("name") == args.name), None)
    if match is None:
        print(f"probe router {args.name!r} NOT FOUND in {args.folder!r}", file=sys.stderr)
        print("If `terraform apply` reported success, that absence IS the finding.",
              file=sys.stderr)
        return 1

    print("=== WHAT SCM ACTUALLY STORED ===")
    print(json.dumps(match, indent=2, sort_keys=True))

    vrfs = match.get("vrf") or []
    vrf = vrfs[0] if vrfs else {}
    routes = _routes_of(vrf)

    def _got(key: str):
        if key == "vrf.interface":
            return vrf.get("interface")
        name = key[key.index("[") + 1:key.index("]")]
        rest = key.split("].", 1)[1]
        route = routes.get(name)
        if route is None:
            return None
        if rest == "nexthop.ip_address":
            return (route.get("nexthop") or {}).get("ip_address")
        return route.get(rest)

    print("\n=== VERDICT ===")
    dropped = []
    for key, want in INTENDED.items():
        got = _got(key)
        ok = got == want
        print(f"  {'HONORED ' if ok else 'DROPPED '} {key}: wanted {want!r}, got {got!r}")
        if not ok:
            dropped.append(key)

    print()
    if not routes:
        print("RESULT: the provider wrote the router but NO ROUTES AT ALL.")
        print("=> This is the silent-convergent failure RouteRequest was built to avoid.")
        print("=> RouteRequest needs an enrich-style REST subsystem, as rules do.")
        return 3
    if dropped:
        print("RESULT: the provider writes scm_logical_router only PARTIALLY.")
        print(f"=> dropped: {dropped}")
        print("=> Those fields need enrich, or must be removed from the kind's surface.")
        return 3
    print("RESULT: the provider writes scm_logical_router faithfully, routes included.")
    print("=> RouteRequest is a compiler + tfvars mapping. No enrich needed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
