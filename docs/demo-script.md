# Demo script — showing the platform in 20 minutes

*How-to — for whoever is presenting. Five acts, typed live. The audience sees a
rule requested, applied, broken by hand, detected, reverted, and evidenced.*

Run first on 2026-10-30 for the Singapore team, which is why this exists: the
flow holds up in front of people, and the parts that could embarrass you are
written down below rather than discovered on the day.

**Fill in before you start:** `TICKET-A` creates the demo rule, `TICKET-B`
removes it afterwards. They must differ — the platform **rejects** a removal
quoting the ticket that created the object, because the evidence would name the
wrong authorisation.

---

## Setup on the machine you present from

The part you type needs almost nothing. `compile` and `classify` never reach SCM
(ADR-0004), so they run on a laptop with no credentials at all.

```bash
git clone https://github.com/martono25/palo-firewall-gitops.git
cd palo-firewall-gitops
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
fwgitops --help
```

```bash
gh auth login     # scopes: repo AND workflow
gh auth status    # confirm both appear
```

| Need | For |
|---|---|
| git, python3.9+, `pip install -e .` | the whole git flow and local validation |
| `gh` with `repo` + `workflow` | opening the PR, dispatching the two jobs |
| nothing else | no SCM credentials, no AWS, no Terraform, no `.pem` |

Two browser tabs open before you begin: the **SCM console** on `prod-edge` →
Security policy, and the repository's **Actions** tab.

---

## Act 1 — A rule request, start to finish (~8 min)

```bash
git checkout main && git pull
```

Create `intent/prod/demo/REQ-2026-0930.yaml`:

```yaml
apiVersion: fw-intent/v1
kind: AccessRequest
metadata:
  id: REQ-2026-0930
  requester: you@corp.com
  ticket: TICKET-A
  justification: "Reporting service access for the app tier — demo"
  requested: 2026-11-16
spec:
  environment: prod
  action: allow
  source:
    - cidr: 10.20.1.0/24
  destination:
    - cidr: 10.20.30.15/32
  service:
    - protocol: tcp
      port: 8443
  log: true
```

**Say while typing:** no folder, no zone, no address object, no rule name. The
requester states intent; the platform decides how.

```bash
fwgitops compile intent
```

Offline, no credentials. This is also the syntax check — delete the `ticket:`
line and re-run to watch it fail in a second naming the file and field, then put
it back.

```bash
fwgitops classify intent | grep 0930
```

```
REQ-2026-0930          LOW       allow_without_inspection
```

**LOW means it applies with no human reviewer.** The finding is honest: the rule
permits traffic with no threat-inspection profile.

Want to show the contrast? Widen the source to `10.20.0.0/16` and re-run:
`broad_source` fires, the tier becomes **HIGH**, and the apply would stop at the
`firewall-apply` gate for a human. Change it back.

```bash
git checkout -b req/REQ-2026-0930
git add intent/prod/demo/REQ-2026-0930.yaml
git commit -m "feat(intent): REQ-2026-0930 — app tier to reporting service (TICKET-A)"
git push -u origin req/REQ-2026-0930
gh pr create --fill
```

CI re-runs the same `compile` and `classify` you just ran, plus a
`terraform plan` naming every SCM object the rule will create.

```bash
gh pr checks --watch
gh pr merge --squash --delete-branch
gh run list --workflow apply.yml --limit 1
gh run watch          # paste the run id
```

The apply creates the address and service objects, applies the rule, enriches
it, pushes to the firewall, and opens an **evidence PR**. Merge that too — an
audit record left open is the thing this platform exists to prevent.

---

## Act 2 — Break it by hand (~2 min)

In the **SCM console**, `prod-edge` → Security policy → **Add**:

| Field | Value |
|---|---|
| Name | `console-hack-demo` |
| Action | **deny** — opens nothing, even for the minutes it exists |
| Source / Destination / Service | any |

Commit it in SCM.

**Say:** nothing stopped me. Console access exists, and that is the problem this
platform addresses — not by removing the console, but by noticing.

---

## Act 3 — Detection (~5 min)

**3.1 — Start the detection run.** It normally runs at 09:00 SGT; dispatching it
by hand runs the same workflow on demand.

```bash
gh workflow run drift-detect.yml --ref main
```

That prints nothing on success. Wait a few seconds for GitHub to create the run,
then capture its id — you will need it twice:

```bash
sleep 10
RUN=$(gh run list --workflow drift-detect.yml --limit 1 --json databaseId --jq '.[0].databaseId')
echo "$RUN"
```

**3.2 — Watch it.** Takes 2–4 minutes.

```bash
gh run watch "$RUN"
```

**3.3 — It finishes RED, and that is the correct outcome.** Say so before anyone
reads it as a mistake: a detection run that found unauthorised config is
supposed to fail, because a green run means "nothing to answer for".

Show what it found:

```bash
gh run view "$RUN" --log | grep -a "unmanaged"
```

```
unmanaged  prod-edge/console-hack-demo — nothing accounts for it
```

**3.4 — Merge the violation record.** The run wrote a JSON record and opened a
pull request for it; `main` takes no direct writes, not even from the pipeline.
That PR is always on the branch `violations/pending`:

```bash
gh pr list --head violations/pending
```

```bash
gh pr merge <number from that list> --squash
```

> Do **not** add `--delete-branch` here. `violations/pending` is reused and
> force-pushed by every detection run; the other PRs in this demo use a fresh
> branch per run, which is why they are merged differently.

Then show the record you just committed:

```bash
git checkout main && git pull
ls evidence/violations/ | tail -3
cat evidence/violations/*console-hack-demo*.json
```

```json
"id": "VIOL-2026-1116-prod-edge-console-hack-demo",
"class": "unmanaged",  "scope": "prod-edge",
"first_seen": "...",   "last_seen": "...",   "status": "open"
```

**Say:** the id is derived from the finding itself — date first seen, scope,
object name — so every run computes the same id without a counter to coordinate.
And this is a **finding, not an event**: seen on ten nights it stays ONE file
whose `last_seen` moves, so "how long was this open?" has an answer. When it is
fixed the status becomes `resolved` and the file stays, because deleting it
would erase the fact that the gap existed.

**Say:** six engines ran, not one — tags for provenance, every field of every
managed rule, rule order, zones/routes/interfaces, address and service objects,
and `terraform plan` as a backstop.

---

## Act 4 — Remediation (~5 min)

**4.1 — Start the remediation run.** It normally runs at 10:00 SGT, an hour
after detection. The flag is not optional:

```bash
gh workflow run remediate.yml --ref main -f dry_run=false
```

> **`-f dry_run=false` is required.** A dispatch defaults to dry-run: without
> it the run reports what it *would* delete, deletes nothing, and you will be
> standing in front of a console that still shows the rule.

```bash
sleep 10
RUN=$(gh run list --workflow remediate.yml --limit 1 --json databaseId --jq '.[0].databaseId')
gh run watch "$RUN"
```

Takes 3–5 minutes. This one finishes **green**.

**4.2 — Show the firewall config in SCM.** Refresh the console tab:
`console-hack-demo` is **gone**. That is the whole claim of the platform, made
visible.

**4.3 — Merge the remediation record.** A second PR carries a
`fw-manual-action/v2` record naming what was deleted and the violation id it
answers. Unlike the violation PR, this one has a per-run branch:

```bash
gh pr list --search "record: removed unauthorised config"
```

```bash
gh pr merge <number from that list> --squash --delete-branch
```

**4.4 — Show the two records linked.**

```bash
git checkout main && git pull
cat "evidence/manual-actions/$(ls -t evidence/manual-actions | head -1)"
```

```json
"schema": "fw-manual-action/v2",
"action": "delete",   "name": "console-hack-demo",
"violation_id": "VIOL-2026-1116-prod-edge-console-hack-demo",
"provenance": "workflow",   "run_url": "https://github.com/...",
"reason": "Remediate unauthorised change — VIOL-..."
```

The action record names the **`violation_id` it answers**, so the finding and
the fix are one chain rather than two unrelated files. `provenance: workflow`
means the pipeline did it, not a person reconstructing it afterwards.

> ### Know this before you promise anything
>
> A **dispatched** remediate **deletes** config Git does not declare. It does
> **not restore** config Git declares that someone edited — the restore job is
> `if: github.event_name == 'schedule'`, so it runs only at 10:00 SGT.
>
> | What you broke | Dispatched remediate |
> |---|---|
> | added a rule by hand (**Act 2**) | deleted, live ✅ |
> | edited an existing managed rule | detected live, **restored only by the nightly run** |
>
> So demo the deletion. If you also edit `REQ-2026-0930` to show detection, say
> the restore happens at 10:00 and show it the next day. Do not promise it live.

**Say:** this normally runs unattended at 10:00 SGT, an hour after detection.
That hour is deliberate — a hand-made emergency fix survives until then.

---

## Act 5 — Evidence (~3 min)

```bash
git checkout main && git pull
cat evidence/prod-edge/REQ-2026-0930.json
```

Walk through: the ticket and justification, the compiled rule exactly as
applied, the risk tier with the findings that produced it, the approvers (and
`controls_not_evidenced` when there were none), the commit that authorised it,
and the push job that delivered it.

```bash
ls evidence/violations/ | tail -3
```

**Close with:** this file answers "who allowed this traffic, and on whose
authority", and it lives in Git rather than a system that expires.

---

## If someone asks whether the firewall is really enforcing

Do not assume it is — this is the failure that looks like success:

```bash
./terraform/preflight.sh --device <mgmt-ip>
```

With the advanced routing engine off, SCM pushes the logical router and the
firewall silently ignores it: interfaces addressed, forwarding `N/A`, no routes,
and every pipeline check green including `device-sync`. It happened on
2026-11-16. The check above is the one that catches it.

---

## Cleanup, away from the audience

```bash
git checkout main && git pull
git checkout -b remove/REQ-2026-0930
git rm intent/prod/demo/REQ-2026-0930.yaml
git commit -m "remove: REQ-2026-0930 — demo rule"
git push -u origin remove/REQ-2026-0930
```

Open the PR and put this **in the PR body** — a squash merge lands the body on
`main`, and that is the text the apply reads:

```
Removes: REQ-2026-0930 (TICKET-B)
```

`TICKET-B`, not `TICKET-A`. Deleting the intent destroys the rule in SCM
(ADR-0008).

---

## If something goes wrong

| Symptom | Cause | Do this |
|---|---|---|
| PR checks sit pending | `AUTOMATION_PR_TOKEN` not usable | the run says which case; approve the checks by hand and carry on |
| `compile` fails | YAML typo | it names the file and field — this is a feature, use it |
| classify says HIGH | the rule is broader than planned | the apply waits at `firewall-apply`: Actions → the run → Review deployments |
| apply fails `403 Access denied` on a plan read | SCM transient, seen 2026-09-15 | re-run the **workflow**, never `terraform` locally |
| drift-detect fails at `device-sync` | the firewall is stopped or absent | harmless here: it reads SCM, not the device |
| the drift run finished **green** | nothing was detected — the hand-made rule was never committed in SCM | commit it in the console, then dispatch drift-detect again |
| `gh pr list --head violations/pending` shows nothing | either the run found no drift (see the row above), or someone already merged it | `gh pr list --state merged --head violations/pending` to check the second case |
| `gh run watch` says "no runs" | the run has not been created yet | wait ten seconds and re-capture `RUN` — GitHub creates it asynchronously |
| you want the device view | needs the `.pem` **and** your current IP in the mgmt security group | ask before the session, or skip it |

## Related

- [tutorial-your-first-rule.md](tutorial-your-first-rule.md) — the same flow at
  a reader's pace, without an audience
- [how-drift-enforcement-works.md](how-drift-enforcement-works.md) — why the
  six engines and the one-hour gap exist
- [operator-runbook.md](operator-runbook.md) — what to do when drift fires for
  real
- [assessor-guide.md](assessor-guide.md) — what the evidence proves
