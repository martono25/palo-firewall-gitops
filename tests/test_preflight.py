"""The account-move prerequisites, as checks rather than prose.

Three moves went wrong in five places and every one was a line in a document
instead of something that ran:

  * "delete the firewall from CSP first"  -> the dead device's root was imported
  * "subscribe in Marketplace"            -> found by a failed `terraform apply`
  * "the key pair must match"             -> a right-named, wrong key; SSH's
                                             "Permission denied" read as a
                                             broken firewall (2026-10-10)
  * "type yes, don't pipe it"             -> `yes` prints "y"; every apply
                                             cancelled, twice
  * advanced routing                      -> written down nowhere until a
                                             firewall routed NOTHING while every
                                             check said it was healthy

These run the shipped shell. Each case is a verdict the next move depends on:
0 pass, 1 blocking, 2 advisory.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[1] / "terraform" / "lib" / "preflight.sh"
PASS, FAIL, WARN = 0, 1, 2


def pf(fn: str, *args: str):
    """Run one shipped check; return (exit code, message)."""
    quoted = " ".join(f'"{a}"' for a in args)
    r = subprocess.run(["bash", "-c", f'. "{LIB}"; {fn} {quoted}'],
                       capture_output=True, text=True)
    return r.returncode, r.stdout.strip()


AWS_FP = "df:de:fa:be:33:c4:db:e5:1f:a8:34:83:3b:88:73:25:40:14:c3:02"
OTHER_FP = "df:53:18:81:f7:ea:fc:95:f4:14:ef:95:78:78:72:09:ef:56:31:0c"


# ── the key pair that had the right name and the wrong key ──────────────────

def test_a_matching_key_passes():
    rc, msg = pf("pf_key_fingerprint", AWS_FP, AWS_FP)
    assert rc == PASS and "matches" in msg


def test_a_DIFFERENT_key_with_the_same_name_is_BLOCKING():
    """2026-10-10: the account already held `fwgitops-pilot`, the launch used it,
    and SSH failed in a way that looks like a broken firewall."""
    rc, msg = pf("pf_key_fingerprint", AWS_FP, OTHER_FP)
    assert rc == FAIL
    assert "KEY MISMATCH" in msg and "cannot SSH" in msg


def test_no_key_pair_in_the_account_is_BLOCKING():
    rc, msg = pf("pf_key_fingerprint", "", AWS_FP)
    assert rc == FAIL and "create it before launching" in msg


def test_no_local_pem_is_BLOCKING_and_names_the_symptom():
    rc, msg = pf("pf_key_fingerprint", AWS_FP, "")
    assert rc == FAIL and "publickey" in msg


def test_an_IMPORTED_key_is_UNCOMPARABLE_not_wrong():
    """AWS fingerprints an imported key differently (md5 of the public key).
    Refusing a launch over a value we cannot compute the same way would be a
    false alarm — and this check exists to remove one."""
    rc, msg = pf("pf_key_fingerprint", "d1:9f:ab:cd", AWS_FP)
    assert rc == WARN and "cannot compare" in msg


# ── the quota ───────────────────────────────────────────────────────────────

def test_a_quota_below_the_instance_is_BLOCKING():
    rc, msg = pf("pf_quota", "2", "4")
    assert rc == FAIL and "will fail" in msg


def test_a_sufficient_quota_passes():
    rc, _ = pf("pf_quota", "64", "4")
    assert rc == PASS


def test_the_exact_quota_passes():
    """5 vCPU and a 4-vCPU firewall is the real new-account case — it fits, and
    reporting it as a failure would send the operator to raise a quota they do
    not need."""
    rc, _ = pf("pf_quota", "5", "4")
    assert rc == PASS


def test_an_unreadable_quota_is_advisory():
    rc, msg = pf("pf_quota", "", "4")
    assert rc == WARN and "by hand" in msg


# ── the Marketplace subscription ────────────────────────────────────────────

def test_the_dry_run_SUCCEEDING_is_the_pass():
    rc, msg = pf("pf_marketplace",
                 "An error occurred (DryRunOperation) ... Request would have succeeded")
    assert rc == PASS and "active" in msg


def test_OptInRequired_is_BLOCKING_with_what_to_do():
    rc, msg = pf("pf_marketplace",
                 "An error occurred (OptInRequired) when calling the RunInstances operation")
    assert rc == FAIL and "subscribe in Marketplace" in msg


def test_wrong_credentials_are_distinguished_from_no_subscription():
    rc, msg = pf("pf_marketplace", "An error occurred (UnauthorizedOperation)")
    assert rc == FAIL and "wrong account" in msg


def test_an_unknown_answer_is_advisory_and_quotes_it():
    rc, msg = pf("pf_marketplace", "something nobody has seen before")
    assert rc == WARN and "something nobody" in msg


# ── the `yes |` trap ───────────────────────────────────────────────────────

def test_a_pipe_into_a_run_that_APPLIES_is_BLOCKING():
    """`yes | ./rebootstrap-account.sh` cancels every apply, because `yes`
    prints "y" and terraform accepts only the literal "yes"."""
    rc, msg = pf("pf_stdin_is_usable", "0", "1")
    assert rc == FAIL
    assert "CANCEL" in msg and "yes yes" in msg


def test_a_terminal_passes():
    rc, _ = pf("pf_stdin_is_usable", "1", "1")
    assert rc == PASS


def test_a_pipe_is_FINE_when_nothing_will_be_applied():
    """The rehearsal applies nothing, so it must stay scriptable."""
    rc, _ = pf("pf_stdin_is_usable", "0", "0")
    assert rc == PASS


# ── credential lifetime ────────────────────────────────────────────────────

def test_temporary_credentials_are_advisory():
    """My own token expired mid-move twice. Advisory, not blocking: it is a
    normal way to work, it just needs saying."""
    rc, msg = pf("pf_credentials_lifetime", "ASIAW5LSCQOO2N24OJ5Z")
    assert rc == WARN and "expire" in msg


def test_long_lived_keys_pass():
    rc, _ = pf("pf_credentials_lifetime", "AKIAW5LSCQOO2N24OJ5Z")
    assert rc == PASS


# ── the CSP-deletion prerequisite, executed ────────────────────────────────

def test_a_DECLARED_firewall_missing_from_SCM_is_BLOCKING():
    """The prose prerequisite. The script's step 2 said "any SKIPPED device root
    is a dead firewall", which tells the operator nothing when nothing was
    skipped because the firewall is still registered."""
    rc, msg = pf("pf_declared_firewalls", "007955000935486", "")
    assert rc == FAIL
    assert "ABSENT from SCM" in msg and "SAME PR" in msg and "NEW ticket" in msg


def test_a_REGISTERED_firewall_nobody_declared_is_BLOCKING():
    rc, msg = pf("pf_declared_firewalls", "", "007955000959850")
    assert rc == FAIL and "adopt-device" in msg


def test_matching_sets_pass():
    rc, _ = pf("pf_declared_firewalls", "007955000959850", "007955000959850")
    assert rc == PASS


def test_an_empty_estate_passes():
    """Between retirement and the next launch there is legitimately neither."""
    rc, msg = pf("pf_declared_firewalls", "", "")
    assert rc == PASS and "nothing to reconcile" in msg


# ── the silent one ─────────────────────────────────────────────────────────
#
# Both strings below are verbatim from 007955000959850 on 2026-11-16, captured
# before and after the reboot that enabled the engine.

OFF = ("Server error :  show -> advanced-routing  is invalid. "
       "advanced routing mode is not enabled")
ON = "ID             NAMESPACE      LOGICAL-ROUTER\n----------------------------"


def test_advanced_routing_OFF_is_BLOCKING_and_gives_the_RIGHT_spelling():
    """2026-11-16: addressed, zoned, every rule present, routing NOTHING, and
    `device-sync` called it current. The KB's `advanced-routing yes` is rejected
    as invalid syntax by PAN-OS 11.2.12; the device's own completions say
    `advance-routing`."""
    rc, msg = pf("pf_advanced_routing", OFF)
    assert rc == FAIL
    assert "IGNORED" in msg
    assert "advance-routing yes" in msg and "advanced-routing yes" not in msg
    assert "reboot" in msg


def test_advanced_routing_ON_passes_on_the_TABLE_HEADER():
    """The probe cannot key on `show system setting advanced-routing`: that
    prints `off` while the engine is off and `Invalid syntax` once it is ON, so
    keying on it reports "could not check" for the healthy firewall it was meant
    to bless. The router command answers in both directions."""
    rc, msg = pf("pf_advanced_routing", ON)
    assert rc == PASS and "ON" in msg


def test_an_EMPTY_router_table_still_counts_as_ON():
    """A fresh firewall has no routers yet. The header alone is the signal —
    requiring a named router would fail every firewall on its first check."""
    rc, _ = pf("pf_advanced_routing", "ID  NAMESPACE  LOGICAL-ROUTER")
    assert rc == PASS


def test_the_old_setting_output_is_still_understood():
    """Kept deliberately: an operator pasting `show system setting
    advanced-routing` from an OFF device must not get a shrug."""
    assert pf("pf_advanced_routing", "advanced-routing: off")[0] == FAIL
    assert pf("pf_advanced_routing", "advanced-routing: on")[0] == PASS


def test_no_device_output_is_advisory_not_a_pass():
    """An unreachable device must never read as a healthy one. The first live
    run hit this: `ssh -T` gets nothing from PAN-OS, which needs a pty."""
    rc, _ = pf("pf_advanced_routing", "")
    assert rc == WARN


def test_every_check_is_exercised_here():
    """A check nobody calls is prose with a shebang."""
    import re
    shipped = set(re.findall(r"^(pf_[a-z_]+)\(\)", LIB.read_text(), re.M))
    tested = set(re.findall(r'pf\("(pf_[a-z_]+)"', Path(__file__).read_text()))
    assert shipped == tested, f"untested: {sorted(shipped - tested)}"
