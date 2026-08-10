# Operator runbook

What to do when the pipeline needs you. **Audience: platform team.** Every
procedure here has been run against the live tenant; where something failed the
first time, it says so.

Commands are in [`cli-reference.md`](cli-reference.md). This page is the tasks.

---

## A run is waiting for you

**Symptom:** a run on `main` sits at `waiting`, and `gh run list` shows the
`apply` job neither started nor failed.

That is the design. `classify` graded the changeset above LOW, so the workflow
routed to the `firewall-apply` environment, which has a required reviewer.

**Check what you are approving before approving it:**

```bash
gh api repos/martono25/palo-firewall-gitops/actions/runs/<run-id>/pending_deployments \
  --jq '.[] | "\(.environment.name) ← \(.reviewers[].reviewer.login)"'
gh run view <run-id> --log | grep -E "will be created|will be destroyed|Plan:"
```

**Then approve** in the run's *Review deployments* panel, or:

```bash
ENV_ID=$(gh api repos/martono25/palo-firewall-gitops/environments/firewall-apply --jq .id)
gh api -X POST repos/martono25/palo-firewall-gitops/actions/runs/<run-id>/pending_deployments \
  -f state=approved -f comment="why you are approving" -F "environment_ids[]=$ENV_ID"
```

> **This gate is the CM-5 claim.** The evidence bundle records who released the
> deployment, by login. Approving a change you have not read makes that record
> true and the control hollow, which is worse than not claiming it.

**If you did not expect a hold:** the tier is computed, never typed. Reproduce
it locally:

```bash
git archive HEAD^ intent | tar -x -C /tmp/base && git log -1 --format=%B > /tmp/msg.txt
fwgitops classify intent --baseline /tmp/base/intent --change-message /tmp/msg.txt
```

That prints the per-change grade and the checks that fired.

---

## Removing a rule

A removal is the operation most likely to surprise you, and the one that broke
twice on 2026-08-10.

1. **Delete the intent file.**
2. **Put a `Removes:` trailer in the PR body** — not only the commit message.
   Squash merge means the PR body is what lands on `main`:

   ```
   Removes: REQ-2026-121 (JIRA-9001)
   ```

   A removal needs **its own ticket**. The intent's `metadata.ticket` authorised
   *creating* the object, and the file it lived in is gone, so there is nowhere
   left to update it.
3. **Expect HIGH.** Deleting a rule is not the inverse of adding one — the
   classifier cannot know whether a deny rule was load-bearing, so it refuses to
   auto-apply the deletion and routes to a reviewer.

**Do not fix a failed removal by dispatching an apply.** A dispatch baselines
against `HEAD^`, where the file is already gone, so the deletion does not
register as a removal at all: Terraform reconciles the rule away and **no
evidence bundle records it**. A silent deletion is the exact failure this
pipeline exists to prevent.

If a removal's apply failed after merging, restore the intent in a PR (a no-op
on the firewall, since the object is still live and still in state), then remove
it again as its own change.

**What removal means per kind** is in
[`adr/0008-deletion-contract.md`](adr/0008-deletion-contract.md), and the
summary is worth having in your head:

| Kind | On removal |
|---|---|
| `AccessRequest` | rule destroyed, then swept tags |
| `ZoneRequest` | **SCM refuses** while any rule references it (409 `NON_ZERO_REFS`) |
| `RouteRequest` | **nothing refuses it, at any layer.** Off-subnet traffic black-holed ~40s after the push reports success |
| `InterfaceRequest` | reverts to the inherited object, which carries no addressing — the firewall loses the IP |

---

## Drift fired

The nightly job failed. That failure **is** the alert.

```bash
gh run view --log --job <job-id> | grep -E "DRIFT|::warning|::error"
```

Three engines report differently:

| Message | Means | First move |
|---|---|---|
| `DRIFT in '<folder>'` | `terraform plan` sees managed config changed in SCM | read the plan; someone edited a managed object out of band |
| `ZONE DRIFT` / state drift | an object exists that Git never declared, or a declared one changed | `fwgitops snapshot <kind> --scope-dir <dir> --out /tmp/s.json` and read it |
| `device-sync` failure | SCM and the **firewall** disagree | the change never reached the device; re-push the scope |

**Reconcile through the pipeline, never by hand.** The fix is a PR that either
declares what someone added or re-applies what Git says. Editing SCM to match
Git leaves no record of either the drift or the correction.

**If the firewall is suspended**, the job is skipped rather than failed, on
purpose — a red run every morning for a known-absent firewall is how a real
alert gets ignored. A manual dispatch always runs.

---

## The evidence PR is sitting open

Every apply that changes something opens `evidence: bundles for <sha>`. **Merge
it.** The apply already happened; the PR is what puts the record in the source
of truth.

If its checks are stuck at `action_required`, `AUTOMATION_PR_TOKEN` is missing
or expired — see below. The run warns when it is absent.

---

## `AUTOMATION_PR_TOKEN` expired

**Symptom:** PRs the pipeline opens have their checks held pending, and the run
log carries `::warning::AUTOMATION_PR_TOKEN is not set`.

Nothing breaks loudly. Applies still run, evidence still generates — it just
stops landing on its own, and an audit record waiting on a click is the
artifact-with-a-TTL problem in a new shape.

**Rotate:** create a fine-grained PAT (this repo only, **Contents: read/write**,
**Pull requests: read/write**, *not* `Issues`), then:

```bash
gh secret set AUTOMATION_PR_TOKEN
```

Paste at the prompt; do not put it on the command line. Full rationale:
[`automation-token.md`](automation-token.md).

**Verify it works** rather than assuming — open a throwaway issue with the
firewall-rule form and confirm the generated PR is authored by your account and
its checks start without you touching them.

---

## Bringing the pilot up and putting it away

The pilot is suspended between sessions to stop the EC2 draw (`m5.4xlarge`,
sized for ENIs rather than CPU).

**Up:**

```bash
aws ec2 start-instances --region ap-southeast-1 --instance-ids i-0feced64ef9b5387f
gh variable set FIREWALL_ONLINE --body true
```

PAN-OS needs roughly ten minutes after the instance reports `running`. Wait for
the control plane, not the hypervisor:

```bash
printf 'set cli pager off\nshow cloud-management-status\n' \
  | ssh -T -i fwgitops-pilot.pem admin@<mgmt-ip>   # want: Connected : yes
```

**Down:**

```bash
gh variable set FIREWALL_ONLINE --body false
aws ec2 stop-instances --region ap-southeast-1 --instance-ids i-0feced64ef9b5387f
```

`FIREWALL_ONLINE=false` skips the nightly drift job. A skipped job is visibly
skipped; silently green would be the worse failure.

**If SSH times out**, your egress IP has changed. Re-point the management
security group at your current `/32` — never widen it.

---

## Break-glass

When a change must reach the firewall and the pipeline cannot deliver it.

**Preferred: dispatch the apply.** Same routing, same evidence, same approval.
No knobs; there is deliberately no input to raise or lower the tier.

```bash
gh workflow run apply.yml
```

**Last resort: push the whole candidate.**

```bash
fwgitops push --scope-dir prod-edge --all-admins --record push-prod-edge.json
```

`--all-admins` commits **everything staged in that folder**, including changes
made outside this platform by anyone. The default admin scoping is what makes a
normal push safe on a shared folder; this removes it.

Afterwards, and in the same session:

```bash
fwgitops evidence intent --out evidence --status applied \
  --push-record push-prod-edge.json --approver <you>:deployment_gate
fwgitops drift --snapshot <snapshot>     # confirm you pushed only what you meant to
```

The bundle records `all_admins: true`, so a break-glass push is visible in the
audit record as a different act from a normal one. That is the point of
recording it rather than the identity.

---

## A change applied but is not on the firewall

Git and SCM agree, the run was green, the device does not have it.

**A successful push does not mean the device has the change.** Measured
2026-08-06: a route disappeared from the device about **40 seconds after the
push reported success**. Anything asserting "it is live" has to poll the device.

```bash
fwgitops device-sync
printf 'set cli pager off\nshow running security-policy\n' \
  | ssh -T -i fwgitops-pilot.pem admin@<mgmt-ip> | grep REQ-
```

If SCM holds it and the device does not, re-push the scope. If neither holds it,
the apply staged nothing — check whether the push was skipped because nothing
was staged, which is normal and logged.

---

## Where to look next

| | |
|---|---|
| Every command and its exit codes | [`cli-reference.md`](cli-reference.md) |
| What the evidence proves, for an auditor | [`assessor-guide.md`](assessor-guide.md) |
| Standing up a new folder | [`building-a-folder.md`](building-a-folder.md) |
| What enforces what | [`GITHUB-SETUP.md`](GITHUB-SETUP.md) |
| Bringing up a new firewall | [`provisioning.md`](provisioning.md) |
