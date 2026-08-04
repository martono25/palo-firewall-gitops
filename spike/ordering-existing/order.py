#!/usr/bin/env python3
"""Print the GitOps rulebase order for the probe rules."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from fwgitops.scmapi import ScmCredentials, ScmSession  # noqa: E402

s = ScmSession(credentials=ScmCredentials.from_env())
d = s.request("GET", "/config/security/v1/security-rules",
              params={"folder": "GitOps", "limit": 200})["data"]
print(", ".join(r["name"].replace("fwgitops-oe-", "")
                for r in d if r["name"].startswith("fwgitops-oe-")) or "(none)")
