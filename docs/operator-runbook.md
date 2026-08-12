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

It should not be. Every apply that changes something opens
`evidence: bundles for <sha>` **with auto-merge enabled**, so it lands on its own
once the required checks pass. If one is still open, something stopped it:

| Cause | Sign |
|---|---|
| checks stuck at `action_required` | `AUTOMATION_PR_TOKEN` missing or expired — see below; the run warns |
| auto-merge could not be enabled | `::warning::could not enable auto-merge` in the run log; check the repository allows it |
| a **conflict** with another run's bundle | the PR shows `CONFLICTING` |

The last one is deliberate: two runs disagreeing about the same bundle is worth a
human, not an auto-resolve that silently drops one change's record. Resolve it by
hand and merge.

**Auto-merge bypasses nothing.** `--auto` waits for the required checks, and the
ruleset still applies — `main` takes no direct push from anyone. What it removes
is a click on a diff of sha256 hashes that nobody was reviewing, and an audit
record sitting outside the source of truth until somebody noticed. One waited a
day before an operator asked why.

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

The pilot is suspended between sessions to stop the EC2 draw (`m5.xlarge` — 4
vCPU, 4 ENIs, which is both this deployment's ceiling and its requirement).

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

## Replacing a firewall (new serial)

**Steps 4-8 are also what you do after building a firewall for the first time.**
They point the repository at a serial — nothing is destroyed, nothing is undone.
If you have just provisioned and are wondering why you are reading a page about
replacement, skip to [step 4](#step-4); that is the bridge between
[`provisioning.md`](provisioning.md) and
[`building-a-folder.md`](building-a-folder.md).

**Why it comes before Day-1.** The Day-1 chain is a set of intents, and an
`InterfaceRequest` names its target firewall by serial:

```yaml
spec:
  device: "007955000902404"     # this firewall, not that one
```

Until that says the serial you actually have, the Day-1 apply targets a device
that does not exist. So the order is: provision → point the repo at the serial →
Day-1 chain → rules.

The serial is threaded through the repository, so a rebuild is not just a
`terraform apply`.

**What IS caught:** an intent naming a firewall the catalog does not declare is
rejected by `compile`, with the file, the field and the fix. Doing step 4 and
forgetting step 5 fails loudly and safely — verified 2026-08-10.

**What is NOT caught:** the catalog's interface **port map** against SCM. Nothing
compares `catalog/interfaces.yaml` to the real `default_value` of `$eth-*`, so a
port that disagrees writes the wrong interface with no error at any stage. That
is why step 4's two halves — the per-serial map and `create_in` — have to move
together and be read twice.

Do it in this order. Steps 1-3 are the rebuild itself and only apply if you are
replacing an existing firewall; steps 4-8 apply either way.

1. **Deactivate the old licence** in the Palo Alto CSP before destroying the
   instance, or the entitlement stays bound to a machine that no longer exists.
2. **Re-point the inherited interface defaults in SCM** at `ngfw-shared`, if you
   are moving to a 4-ENI layout:

   | Variable | From | To |
   |---|---|---|
   | `$eth-local` | `ethernet1/4` | `ethernet1/1` |
   | `$eth-internet` | `ethernet1/3` | `ethernet1/2` |

   These are SCM defaults **inherited by every firewall under `ngfw-shared`**,
   not objects this platform owns. Changing them moves which physical port every
   zone binds, which is free on a firewall that does not exist yet and disruptive
   on one carrying traffic. Do it while the old instance is down.

3. **Destroy and rebuild**, following [`provisioning.md`](provisioning.md).
   Capture the new serial from `show system info`.
<a id="step-4"></a>

4. **Point the repository at the serial — one command.**

   ```sh
   fwgitops adopt-device <new-serial> --folder prod-edge --check      # read it
   fwgitops adopt-device <new-serial> --folder prod-edge --replacing <old-serial>
   ```

   It reads SCM and writes what SCM says: the folder, the `display_name`, and the
   **physical port behind every interface role**, plus the serial across both
   catalogs and every device-scoped intent.

   Those values used to be typed. Each one transcribed something SCM already
   knew, and the port map is the one where a typo does real damage — nothing
   compared `catalog/interfaces.yaml` to the tenant, so a wrong port configured
   the wrong interface with no error at any stage. A value read from SCM cannot
   disagree with SCM.

   **It refuses rather than guesses.** Exit 3 if SCM has not placed the device,
   or has it in a different folder — naming the folder it actually found. A role
   whose variable will not resolve is reported unmapped, not defaulted.

   **Re-run it any time.** Against the same serial it is a no-op if the catalog
   matches, and a correction if it has drifted. That is now the check that
   never existed.

   <details><summary>What it writes, if you want to read the diff first</summary>

   `catalog/folders.yaml` — the serial key and `display_name`. A stale name is
   why `verify-catalog` reports a note worded for the *dangerous* cause: a device
   reset to `PA-VM` means a re-onboard wiped device-scope config. The check
   cannot tell which side moved, which is why it makes you look.

   `catalog/interfaces.yaml` — the per-serial port map for every role. If you
   edit this by hand instead, `create_in` must move with it: they are the pair
   that puts two zones on one physical port when they disagree.

   `intent/**` — `spec.device` on every `InterfaceRequest`. Only that kind
   carries a serial: an interface address belongs to ONE firewall, while
   `ZoneRequest` and `RouteRequest` are folder-scoped policy a new firewall
   inherits. The compiler refuses `device:` on those (ADR-0006).

   </details>

   ```sh
   fwgitops verify-catalog     # expect OK; read every NOTE before believing it
   ```

5. **Rename the intent directory if you want to** — `intent/prod/edge-fw-<last4>/`
   is cosmetic. Nothing reads it; the compiler reads file contents. Rename it to
   match the new serial or leave it. The only difference is whether it lies.

6. **The Terraform roots and the supporting files are done for you.** Step 4
   scaffolds the new device root, removes the old one — including the gitignored
   files `git rm` leaves behind — and follows the serial through `tests/` and the
   guides, which are what break CI.

   `docs/adr/` and `evidence/` are **never** rewritten. An ADR records a decision
   made at a time, and an evidence bundle is the audit trail of a change that
   really happened on a firewall that really existed; a rebuild does not un-happen
   either.

7. **The old state object is still yours to delete.** It is the one step left
   manual, deliberately: irreversible and *remote*, which is the difference
   between a command editing your repository and a command reaching into your
   cloud account to destroy a record.

   ```sh
   aws s3 rm s3://<state-bucket>/device-<old-serial>/terraform.tfstate
   ```

   Or opt in and let step 4 do it:

   ```sh
   fwgitops adopt-device <new> --folder <f> --replacing <old> --prune-state
   ```

   The bucket is read from a root's `backend.hcl` rather than guessed — deleting
   from the wrong bucket is not a mistake worth risking to save a flag. If it
   cannot be read, the command warns and leaves the object alone.

8. **Leave the old evidence bundles alone.** `evidence/device-<old-serial>/` is
   the audit record of changes that really happened on a firewall that really
   existed. Deleting them to tidy up destroys history; they are supposed to
   outlive the device.

9. **Expect SCM commit errors until Day-1 runs.** A new firewall inherits the
   folder's policy the instant it joins — zones, the logical router, rules, all
   folder-scoped and untouched by the rebuild. Interface addressing is
   device-scoped and died with the old firewall, so SCM validates an inherited
   route against a device that has no interface in the nexthop's subnet:

   ```
   can't find interface in 'default' for next hop 10.100.2.1
   ```

   **Nothing is wrong.** That is ADR-0002's chain — interfaces before routes —
   seen from the other end: after a replacement the routes already exist and the
   interfaces are what is missing. Confirm the shape rather than assume it:

   ```sh
   ssh admin@<mgmt-ip> 'show interface all'    # "configured hardware interfaces: 0"
   grep -A3 '^  ip:' intent/<folder>/*/REQ-*.yaml   # the address that will fix it
   ```

   If the nexthop is inside a subnet one of those intents assigns, applying Day-1
   clears it. If it is not, the route and the addressing genuinely disagree and
   the rebuild changed the topology.

10. **Verify before trusting it:**

   ```sh
   fwgitops verify-catalog          # catalog vs SCM's real hierarchy
   fwgitops compile intent --check  # every intent still resolves
   fwgitops device-sync             # SCM vs the running config
   ```

> **The gap to know about.** `verify-catalog` checks the folder hierarchy. It
> does **not** compare the interface port map against SCM, and nothing else
> does either — so a catalog that disagrees with `$eth-*`'s real `default_value`
> writes the wrong port with no error at any stage. Until that check exists,
> step 2 and step 4 have to be done together and read twice.

### `first-push-pending` on a new firewall is not a problem

`device-sync` on a freshly provisioned device reports:

```
first-push-pending  running=v85  committed=v85  folder=prod-edge
NOTE  … SCM still reports is_first_push_done=false …
```

**Ignore it.** The flag predicts nothing on this tenant. It did not clear after
several successful pushes, and on 2026-08-11 the pipeline's normal ADMIN-SCOPED
push to a brand-new firewall reporting `false` succeeded first time — device-scope
job 202, three interfaces committed and verified on the device.

It is reported because SCM exposes it and you will see it in the output, not
because it blocks anything. The authoritative signal is the version comparison
beside it: `running=` equal to `committed=` means the firewall has the config.

> This entry previously said the first push "may be refused" and told you to
> reach for `--all-admins`. That came from a comment in `devicesync.py`, not from
> a measurement, and the measurement contradicted it. A warning for a failure
> that never arrives is how a real one gets ignored — and `--all-admins` commits
> everything staged in a scope, which is not something to reach for on a hunch.

---

### → NEXT: configure the firewall from Git

The repository now names your firewall, and `compile --check` passes. The device
still has **no interfaces, no zones and no routes** — that is the Day-1 chain, and
it has not run.

Go to [`building-a-folder.md`](building-a-folder.md). If the folder already
exists and you are only re-pointing it at a rebuilt firewall, you can go straight
to [§ Applying the chain](building-a-folder.md#applying-the-chain), which is the
commit-PR-merge sequence that actually ships it.

**Expect to give the changed intents new tickets.** You edited existing files
rather than adding them, and a changed `spec` carrying the old
`metadata.ticket` is rejected.

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
