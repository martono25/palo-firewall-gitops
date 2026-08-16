# Assessor guide — what the evidence proves, and how to check it

**Audience: an auditor, assessor or incident responder** with no access to this
platform's CI, its ticketing system, or the people who run it. Everything below
can be done with a clone of this repository and, where noted, read-only access
to the firewall.

The claim this platform makes is narrow and worth stating plainly:

> Every change to the firewall is declared in Git, risk-assessed by a versioned
> classifier, delivered by a recorded push, and accompanied by a machine-written
> record naming who authorised it — and where a control was **not** operating,
> the record says so rather than staying silent.

What it does **not** claim is at the end. Read that part too.

---

## Start here: pick any rule on the firewall

```sh
git clone https://github.com/martono25/palo-firewall-gitops && cd palo-firewall-gitops
fwgitops where REQ-2026-0725
```

That answers "who asked for this, why, under what ticket, and what checked it" in
one command. Exit code `4` means **nothing in this repository accounts for it** —
which is itself a finding, and the honest answer rather than a guess.

Everything after this is verifying that answer rather than trusting it.

---

## Anatomy of a bundle

`evidence/<scope>/<REQ-id>.json`, schema `fw-evidence/v2`. One file per request,
overwritten in place — so `git log` on that one path is that request's whole
life, from creation to removal.

```sh
git log --oneline evidence/prod-edge/REQ-2026-121.json
```

| Section | Field | What it asserts |
|---|---|---|
| top | `schema`, `kind`, `req_id`, `status`, `generated_at` | which schema, which intent kind, and the outcome: `applied` / `rejected` / `failed` / `removed` |
| `request` | `requester`, `ticket`, `justification`, `requested`, `intent_file`, `intent_sha256` | **paperwork only.** Who asked and why, plus a hash of the file they wrote |
| `compiled` | `scope`, `object`, `object_sha256`, `object_is`, `compiler_version`, `tfvars_file`, `tfvars_sha256` | **behaviour.** What the firewall was actually told to do, derived from the spec |
| `risk` | `tier`, `checks_fired`, `classifier_version`, `thresholds_version` | which rules examined this change, what they found, and which version of them |
| `approval` | `approvers[].login`, `approvers[].via`, `pr`, `merge_commit`, `gate` | who authorised it and **which act** they performed |
| `apply` | `run_url`, `plan_sha256` | the CI run that did it |
| `push` | `folder`, `status`, `job_id`, `admin_count`, `all_admins` | the SCM commit that delivered it |
| `removal` | `ticket`, `commit`, `authorises` | on a tombstone: the ticket authorising the **deletion** |
| `controls` / `controls_not_evidenced` | NIST SP 800-53 Rev.5 control ids | what this record claims, and what it deliberately does not |

**`request` and `compiled` are separated on purpose.** Paperwork is what a human
typed; behaviour is derived from the spec and cannot silently disagree with it.
Mixing them is what once let an edited rule keep the ticket that authorised its
previous version.

---

## Verifying a claim without trusting us

### 1. The intent file has not been altered since it was applied

```sh
python3 -c "import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" \
  intent/prod/payments/REQ-2026-0725.yaml
```

Compare to `request.intent_sha256`. A mismatch means the file changed after the
bundle was written — and `git log` on the intent file will show when.

### 2. The change was reviewed before it was applied

```sh
git log --format='%H %s' --grep "REQ-2026-0725"      # find the merge
gh pr view <number> --json reviews,mergedBy,mergeCommit
```

Cross-check `approval.merge_commit` and `approval.pr`. `main` accepts no direct
push — see [`GITHUB-SETUP.md`](GITHUB-SETUP.md) for how to test that yourself,
in the direction that should fail.

### 3. The risk assessment was a real decision, not a label

`risk.checks_fired` lists the named checks that examined this change, and
`risk.classifier_version` / `thresholds_version` pin *which* ruleset did it. So a
past decision stays reproducible after the rules change:

```sh
git log -S '"classifier_version"' --oneline -- src/fwgitops/
fwgitops classify intent            # re-run today's classifier over the tree
```

A tier with an empty `checks_fired` means nothing matched, not that nothing was
checked.

### 4. The change actually reached the firewall

`push.job_id` is the SCM commit job. `push.status: success` means SCM accepted
it. **That is not the same as the device running it** — see "What this does not
claim".

```sh
fwgitops device-sync        # SCM versus the running config
```

### 5. Nothing was applied that Git never declared

```sh
fwgitops drift --snapshot <snapshot>
fwgitops where <any address or object name>     # exit 4 = unaccounted for
```

---

## Controls, and the ones deliberately not claimed

Listing a control here is a claim that it **was operating for this change** —
not that a process document exists.

**Unconditional**, because they hold from the record's own contents:

| Control | Evidenced by |
|---|---|
| `AC-4` information flow enforcement | the rule *is* the flow control; `compiled.object` is what was enforced |
| `CM-3` configuration change control | ticket, justification, PR, merge commit, reviewed before merge |
| `AU-2` / `AU-12` audit events and record generation | the bundle itself, committed and hash-linked |
| `SC-7` boundary protection | the object is a boundary control on a boundary device |

**Conditional — absent when not earned, and the absence is named:**

- **`CM-5` access restrictions for change** — claimed only when an approver is
  recorded. When it is absent, `controls_not_evidenced` says why. It was
  unconditional until v1.38.0 while `approvers` was hard-coded empty, so every
  bundle claimed a control and answered nobody. That is the defect this
  structure exists to prevent.
- **`AC-5` separation of duties** — CRITICAL-tier dual-control changes only.

`approvers[].via` distinguishes `pull_request_review` (reviewed the proposed
change) from `deployment_gate` (released the deployment). **One person doing
both is a finding, not a detail** — and on this deployment, with a single
collaborator, that is exactly what you will see.

---

## What this does not claim

Read this section as carefully as the last one. Each item is a real limit, not a
disclaimer.

**`AC-5` is not earned on this deployment.** One person authors, approves and
releases. CRITICAL routes to the same reviewed environment as HIGH, so a
CRITICAL change has one approver, not two. Genuine dual control needs a second
human, not more code.

**RULE ORDER IS NOT VERIFIED, AND REORDERING IS NOT DETECTED.** On a firewall,
order is policy: evaluation is first-match-wins, so moving a broad `allow` above
a narrow `deny` inverts both without altering a single field. Nothing in this
platform sees that happen.

Demonstrated 2026-08-16 on the live tenant. A rule was moved four places up the
`prod-edge` pre-rulebase in the SCM console, and the full drift job — Terraform
plan, tag-based rule drift, state drift, catalog check — reported **no drift and
a green run**.

It is not an unwired check; it cannot work as built. `relative_position` is a
create/update INSTRUCTION that the provider never reads back, so a plan has
nothing to diff. And `spec.position` is optional and unset on every rule
currently declared, so Git states no order to compare against even in principle.

What the evidence therefore supports: each rule's MATCH and ACTION were
authorised, approved and applied as recorded. What it does not support: that the
rulebase evaluates in the order anyone intended, or that the order has not been
changed since.

**`push.status: success` proves SCM accepted the change, not that the firewall
is running it.** Measured 2026-08-06: a route disappeared from the device about
40 seconds after the push reported success. The scheduled `device-sync` job
compares SCM to the device precisely because this gap is real; a bundle alone
cannot close it.

**`plan_sha256` may be `null`.** Where it is, the Terraform plan for that change
was not hashed into the record, and the `run_url` is the only link to what was
planned. CI logs expire.

**Evidence lands minutes after the change, by pull request.** `main` takes no
direct push from anyone, including the pipeline, so a bundle appears in its own
PR after the apply. An unmerged evidence PR means a change is applied whose
record is not yet in the source of truth. Check for open ones:

```sh
gh pr list --search "evidence: bundles"
```

**The record covers what this platform changed.** A change made directly in the
SCM console is not in Git and produces no bundle. That is what `drift`,
`device-sync` and `verify-catalog` are for — a clean drift run is part of the
evidence, and its absence is a gap in coverage rather than proof of none.

**Failures are recorded, so absence of a `failed` bundle is not proof of no
failures** — it is proof of no *recorded* failures. Look at CI run history for
the period you are assessing.

---

## Where to look next

| | |
|---|---|
| Bundle internals and design rationale | [`../evidence/README.md`](../evidence/README.md) |
| What enforces what, and how each was verified | [`GITHUB-SETUP.md`](GITHUB-SETUP.md) |
| Commands used above | [`cli-reference.md`](cli-reference.md) |
| What removal means per kind | [`adr/0008-deletion-contract.md`](adr/0008-deletion-contract.md) |
| Design decisions, with trade-offs | [`DESIGN.md`](DESIGN.md), [`adr/`](adr/) |
