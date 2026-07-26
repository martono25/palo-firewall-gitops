"""Set the firewall admin password post-boot (route B).

AWS VM-Series defaults the admin account to KEY-ONLY (`phash *`) — password login
is disabled, access is via the EC2 SSH key. So there is no `admin/admin` default
to exploit and no exposure window to close. We simply ADD a password after boot
over SSH, which touches nothing else — avoiding the full-config clobber risk of a
`bootstrap.xml` (which would replace the running config, incl. the injected EC2
key and system users, and could regress SCM auto-onboarding).

The password is supplied as a pre-computed `$5$` SHA-256 phash (openssl passwd -5
or PAN-OS `request password-hash`), so the plaintext is never sent. PAN-OS accepts
a phash directly: `set mgt-config users admin phash <hash>`. The SSH call runs
behind an injectable `runner` so the orchestration is unit-testable.
"""

from __future__ import annotations

import subprocess
from typing import Callable, List

#: PAN-OS prints this on a successful commit; we refuse to claim success without it.
_COMMIT_OK = "committed successfully"


class AdminPasswordError(Exception):
    """Setting the admin password failed (ssh error, or commit not confirmed)."""


def _ssh_argv(mgmt_ip: str, ssh_key: str, user: str) -> List[str]:
    return [
        "ssh", "-i", ssh_key,
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=20",
        "-T", f"{user}@{mgmt_ip}",
    ]


def set_admin_phash(
    mgmt_ip: str,
    phash: str,
    *,
    ssh_key: str,
    user: str = "admin",
    runner: Callable[..., "subprocess.CompletedProcess"] = subprocess.run,
    timeout: int = 120,
) -> None:
    """Set `user`'s password to `phash` on the firewall at `mgmt_ip`, via SSH.

    `phash` must be a crypt hash (e.g. `$5$...`), never a plaintext password —
    PAN-OS stores the hash directly. Raises AdminPasswordError on ssh failure or
    if the commit success line is not seen (never claim a success we cannot see).
    """
    if not phash.startswith("$"):
        raise AdminPasswordError(
            f"phash {phash!r} is not a crypt hash — expected a $5$… value "
            "(openssl passwd -5 / PAN-OS 'request password-hash'), not plaintext"
        )

    # Configure-mode commands fed over stdin (keeps the phash out of argv/ps).
    script = "\n".join([
        "configure",
        f"set mgt-config users {user} phash {phash}",
        "commit",
        "exit",   # leave configure mode
        "exit",   # close the session
    ]) + "\n"

    try:
        proc = runner(
            _ssh_argv(mgmt_ip, ssh_key, user),
            input=script, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:  # pragma: no cover - passthrough
        raise AdminPasswordError(f"ssh to {mgmt_ip} timed out after {timeout}s") from e

    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        raise AdminPasswordError(
            f"ssh to {mgmt_ip} failed (rc={proc.returncode}); output tail: {out[-300:]}"
        )
    if _COMMIT_OK not in out.lower():
        # VERIFY (live): confirm PAN-OS 11.2's exact commit-success phrasing and
        # tighten _COMMIT_OK if needed. Fail closed rather than assume success.
        raise AdminPasswordError(
            f"could not confirm the commit succeeded on {mgmt_ip}; output tail: {out[-300:]}"
        )
