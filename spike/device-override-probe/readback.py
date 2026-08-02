#!/usr/bin/env python3
"""Did a device-scope write to an EXISTING inherited interface override or mutate?

Diffs every scope against a baseline captured before the apply, so "nothing else
changed" is demonstrated rather than asserted.

    python3 spike/device-override-probe/readback.py --baseline /tmp/ovr/baseline.json
    python3 spike/device-override-probe/readback.py --baseline ... --expect-restored

`--expect-restored` flips the pass condition: every scope must match the
baseline exactly (used after destroy).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from fwgitops.scmapi import ScmApiError, ScmCredentials, ScmSession  # noqa: E402

SCOPES = {
    "dev-target": {"device": "007955000893662"},
    "dev-other": {"device": "007955000894453"},
    "folder-shared": {"folder": "ngfw-shared"},
    "folder-prod": {"folder": "prod-edge"},
}
SHARED_ID = "35479f59"   # $eth-local / ethernet1/4 — the inherited object
TARGET_NAMES = {"ethernet1/4", "$eth-local"}


def _snapshot(session):
    out = {}
    for label, scope in SCOPES.items():
        params = dict(scope)
        params["limit"] = 200
        out[label] = session.request(
            "GET", "/config/network/v1/ethernet-interfaces", params=params
        ).get("data", [])
    return out


def _index(rows):
    return {r["name"]: r for r in rows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--expect-restored", action="store_true")
    args = ap.parse_args()

    baseline = json.loads(Path(args.baseline).read_text())
    try:
        now = _snapshot(ScmSession(credentials=ScmCredentials.from_env()))
    except ScmApiError as e:
        print(f"readback failed: {e}", file=sys.stderr)
        return 1

    changed = {}
    for label in SCOPES:
        was, is_ = _index(baseline[label]), _index(now[label])
        for name in sorted(set(was) | set(is_)):
            b, a = was.get(name), is_.get(name)
            if b != a:
                changed[f"{label}/{name}"] = (b, a)

    print("=== SCOPE-BY-SCOPE DIFF vs baseline ===")
    for label in SCOPES:
        rows = _index(now[label])
        for name in sorted(rows):
            r = rows[name]
            mark = "CHANGED" if f"{label}/{name}" in changed else "same   "
            print(f"  {mark} {label:14} {name:14} id={r['id'][:8]} layer3={r.get('layer3')}")

    if args.expect_restored:
        print()
        if changed:
            print("RESULT: NOT RESTORED — these differ from the baseline:")
            for k, (b, a) in changed.items():
                print(f"  {k}:\n    was {json.dumps(b, sort_keys=True)}\n    now {json.dumps(a, sort_keys=True)}")
            return 3
        print("RESULT: tenant fully restored — every scope matches the baseline.")
        return 0

    print("\n=== VERDICT ===")
    if not changed:
        print("  Nothing changed anywhere. The write did not land at all.")
        return 3

    leaked = {k: v for k, v in changed.items()
              if not k.startswith("dev-target/") and Path(k).name in TARGET_NAMES}
    for k, (b, a) in changed.items():
        bl3 = (b or {}).get("layer3")
        al3 = (a or {}).get("layer3")
        bid = (b or {}).get("id", "")[:8] or "-"
        aid = (a or {}).get("id", "")[:8] or "-"
        print(f"  {k}: layer3 {bl3} -> {al3}   id {bid} -> {aid}")

    print()
    if leaked:
        print("RESULT: MUTATED THE SHARED OBJECT. A device-scope write to an existing")
        print("        inherited interface is NOT an override — it changed the object")
        print(f"        seen at: {sorted(leaked)}")
        print("=> `device:` targeting CANNOT isolate interface addressing.")
        print("=> InterfaceRequest must stay folder-scoped; re-scope before building.")
        return 3

    tgt = _index(now["dev-target"]).get("ethernet1/4") or {}
    new_id = tgt.get("id", "")
    print("RESULT: PER-DEVICE OVERRIDE. Only the target device's view changed;")
    print("        the shared object and the other firewall are untouched.")
    print(f"        target object id is now {new_id[:8]} "
          f"({'UNCHANGED — same object, still isolated in effect' if new_id.startswith(SHARED_ID) else 'NEW — a distinct override object'})")
    print("=> `device:` targeting CAN isolate interface addressing. Safe to build.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
