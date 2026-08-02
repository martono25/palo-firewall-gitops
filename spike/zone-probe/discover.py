#!/usr/bin/env python3
"""T5 preflight — READ-ONLY discovery against the SCM tenant.

Writes nothing. Answers three things before we create any object:

  1. Which folders exist (so we pick a scratch folder, not prod-edge).
  2. Which zone-protection profiles exist (phase-2 input; a made-up name is
     rejected with INVALID_REFERENCE and would teach us nothing).
  3. Which log-forwarding profiles exist (phase-2 input).

Run from the repo root:
    export SCM_CLIENT_ID='<name>@<tsg>.iam.panserviceaccount.com'
    export SCM_CLIENT_SECRET=...
    export SCM_SCOPE='tsg_id:<TSG_ID>'
    python3 <path>/discover.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from fwgitops.scmapi import (  # noqa: E402
    ScmApiError,
    ScmAuthError,
    ScmCredentials,
    ScmSession,
)

# The zone-protection path is the uncertain one; try plausible shapes and report
# which answered rather than guessing at the API surface.
CANDIDATES = {
    "folders": ["/config/setup/v1/folders"],
    "existing_zones": ["/config/network/v1/zones"],
    "zone_protection_profiles": [
        "/config/network/v1/zone-protection-profiles",
        "/config/objects/v1/zone-protection-profiles",
    ],
    "log_forwarding_profiles": [
        "/config/objects/v1/log-forwarding-profiles",
    ],
}


def main() -> int:
    try:
        creds = ScmCredentials.from_env()
    except Exception as e:  # ScmConfigError and friends
        print(f"CREDENTIALS NOT USABLE: {e}", file=sys.stderr)
        print(
            "\nSet these in your own shell (do not paste secrets into chat):\n"
            "  export SCM_CLIENT_ID='<name>@<tsg>.iam.panserviceaccount.com'\n"
            "  export SCM_CLIENT_SECRET='...'\n"
            "  export SCM_SCOPE='tsg_id:<TSG_ID>'",
            file=sys.stderr,
        )
        return 2

    session = ScmSession(credentials=creds)
    findings: dict = {}

    for label, paths in CANDIDATES.items():
        attempts = []
        for path in paths:
            try:
                payload = session.request("GET", path, params={"limit": 200})
            except ScmAuthError as e:
                print(f"AUTH FAILED: {e}", file=sys.stderr)
                return 2
            except ScmApiError as e:
                attempts.append({"path": path, "error": str(e)[:200]})
                continue
            except Exception as e:  # noqa: BLE001 - discovery is best-effort
                attempts.append({"path": path, "error": repr(e)[:200]})
                continue

            items = payload.get("data", [])
            names = [i.get("name") for i in items if isinstance(i, dict) and i.get("name")]
            findings[label] = {"path": path, "count": len(names), "names": names[:50]}
            break
        else:
            findings[label] = {"unavailable": attempts}

    print(json.dumps(findings, indent=2, sort_keys=True))

    zp = findings.get("zone_protection_profiles", {})
    if isinstance(zp, dict) and zp.get("names"):
        print(f"\nPHASE 2 RUNNABLE — zone_protection_profile candidate: {zp['names'][0]!r}")
    else:
        print(
            "\nPHASE 2 BLOCKED — no zone-protection profile found (or the endpoint "
            "differs).\nPhase 1 (the booleans) still answers the core question on its own."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
