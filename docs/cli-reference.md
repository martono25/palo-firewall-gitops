# `fwgitops` — command reference

All 21 subcommands, what each answers, and what its exit code means. **Audience:
platform team.** If you want a firewall rule, you want
[`requesting-rules.md`](requesting-rules.md) — none of this is needed to ask for
one.

Generated against v2.2.0. A test asserts this page lists every registered
subcommand, so a new command cannot ship undocumented.

---

## Exit codes are a contract

CI branches on these, so they are stable and worth knowing before the commands.

| Code | Means |
|---|---|
| `0` | success, or a no-op that is not an error |
| `1` | usage, IO, config or auth failure — you did something wrong, or a credential is missing |
| `2` | **invalid input** — an intent failed validation, a form could not be parsed, a contract check failed |
| `3` | **the remote operation failed** — a push refused, an onboard failed, a rule was absent |
| `4` | no match (`where` only — a question with a legitimate empty answer) |

`2` and `3` are deliberately distinct: `2` means the repository is wrong and a
human must edit it, `3` means the repository is fine and something out there
said no.

**Everything is fail-closed and all-or-nothing.** If any intent is invalid,
`compile` writes nothing and exits 2. A partial write would leave Terraform
holding half a change.

---

## Reading and writing intent

### `compile`

Intent YAML → `*.auto.tfvars.json`, one file per Terraform root.

```sh
fwgitops compile intent --out terraform
fwgitops compile intent --check          # validate only, write nothing
```

| Flag | Default | Effect |
|---|---|---|
| `--env-map` | `catalog/environments.yaml` | environment → folder/zone map |
| `--out` | `terraform` | root of the Terraform tree to write into |
| `--check` | off | validate and write nothing; what CI runs on every PR |
| `--allow-missing-root` | off | permit a scope with no Terraform root. **Only for tests** — in normal use a missing root is the ADR-0004 contract failure this flag hides |
| `--service-catalog` / `--app-catalog` | `catalog/services.yaml`, `catalog/apps.yaml` | name → port and name → address resolution |

Compiling data that no Terraform module declares is an **error, not a silent
no-op** (ADR-0004, HOLE 3). Terraform discards undeclared object attributes at
the module boundary with no warning and exit 0, so the check has to happen here.

### `from-issue`

A filled Issue Form → an intent file. This is the intake; the workflow runs it,
and you can run it locally against a saved issue body.

```sh
fwgitops from-issue --body-file issue.md --issue-number 142 --author martono25
```

| Flag | Required | Effect |
|---|---|---|
| `--body-file` | yes | the issue body as GitHub rendered it |
| `--issue-number` | yes | becomes `REQ-<year>-<number>` |
| `--author` | yes | the requester. Taken from the issue author, never a form field — a field someone types is a field someone can type wrongly |
| `--out` | `intent` | where the generated file lands |
| `--check` | off | parse and report, write nothing |

Exit 2 means the form could not be turned into a request, and stdout names the
**form field** to fix, not a schema path.

### `where`

Which intent authorised this? The incident-response command.

```sh
fwgitops where 10.20.1.55
fwgitops where JIRA-12345 --json
```

Takes an address, an object name or a ticket. Exit `4` means no intent accounts
for it — which is itself the finding: something reached the firewall from
outside this repository, and [`drift`](#drift) is the next call.

---

## Deciding what a change is worth

### `classify`

Risk-tier every change. Policy-as-code, no commercial tool involved.

```sh
fwgitops classify intent                                   # per-change report
fwgitops classify intent --max-tier --baseline /tmp/base/intent \
                        --change-message /tmp/msg.txt      # what CI runs
```

| Flag | Effect |
|---|---|
| `--max-tier` | print **one line** — the highest tier in the changeset — and nothing else, so a workflow can assign it directly to a GitHub output |
| `--baseline` | the base revision's intent tree, materialised with `git archive`. **Without it a removal is invisible**: a deleted intent is absent from the current tree, so there is nothing left to classify |
| `--change-message` | the text that lands on `main`. Read for `Removes: <REQ-id> (TICKET)` trailers — a removal authorises itself there, because the change *is* the deletion of the file |
| `--gate {LOW,HIGH,CRITICAL}` | exit 3 if any change exceeds this tier. Legacy; the pipeline routes by tier instead of blocking |
| `--state-snapshot` | include live objects that carry no `gitops:` tag |

Two failure modes worth knowing, because both shipped and both were silent:
`--max-tier` without `--baseline` maximises over every intent that **exists**,
so one permanently-HIGH route makes LOW unreachable; and `--max-tier` without
`--change-message` rejects every removal it cannot see authorised.

### `kinds`

The intent-kind registry, for scripting CI.

```sh
fwgitops kinds --order          # the ordered Day-1 chain
fwgitops kinds --state-drift    # kinds whose drift engine is state-based
```

Ask this rather than hard-coding an order. It comes from `depends_on_kinds` in
the registry and the apply pipeline consumes the same list, so it cannot drift
from what actually runs.

### `apply-order`

Terraform roots in dependency order.

```sh
fwgitops apply-order
```

Exit 2 when kinds are interleaved across roots such that no whole-root order
works — it fails rather than picking one and hoping.

---

## Applying and delivering

### `enrich`

Write the security-rule fields the `scm` provider silently drops (ADR-0003).

```sh
fwgitops enrich prod-edge intent
fwgitops enrich prod-edge intent --dry-run
```

Runs **after** `terraform apply` and **before** `push`, so the skeleton and the
enrichment land in the same candidate and commit atomically.

### `push`

Commit a scope's staged config in SCM.

```sh
fwgitops push --scope-dir prod-edge --record push-prod-edge.json
fwgitops push --device 007955000901881
```

| Flag | Effect |
|---|---|
| `--scope-dir` | a Terraform root directory (`prod-edge` or `device-<serial>`), resolved to the right scope. Prefer this — stripping the `device-` prefix by hand is what broke the drift job in v1.34.2 |
| `--admin` | identity whose staged changes to commit, repeatable. Defaults to `SCM_CLIENT_ID`, which makes the push **safe by construction** on a shared folder |
| `--all-admins` | break-glass: commit the whole candidate, including changes made outside this platform |
| `--record FILE` | write the outcome for `evidence --push-record`. Without it the bundle cannot say the change reached SCM |

Exit 3 is a refused push. That is a **fail-closed success**: it means SCM held
something this platform did not stage.

### `tags`

Create the tag objects a rule references; remove unreferenced ones.

```sh
fwgitops tags ensure prod-edge
fwgitops tags sweep prod-edge
```

`ensure` runs before apply, `sweep` after push, and **never in the same
operation as the rule change that released the tag** — that ordering is the fix
(ADR-0009). Terraform ran a tag destroy before the rule update that freed it and
409'd; the halves are now separated in time.

`sweep` only touches `gitops:` tags, only removes one when nothing references
it, reads references from SCM rather than inferring them, and sweeps nothing if
that read fails.

---

## Evidence

### `evidence`

Write one NIST-mapped bundle per change.

```sh
fwgitops evidence intent --out evidence --status applied \
  --baseline /tmp/base/intent --change-message /tmp/msg.txt \
  --push-record push-prod-edge.json --approver martono25:deployment_gate
```

| Flag | Effect |
|---|---|
| `--status {applied,rejected,failed}` | the outcome recorded. Failures are evidence too |
| `--baseline` | **without it, removals produce no record at all** |
| `--change-message` | supplies the `Removes:` trailer authorising a deletion |
| `--approver LOGIN[:VIA]` | repeatable. `VIA` is `pull_request_review` or `deployment_gate`. **Without at least one, the bundle does not claim NIST CM-5** — that control is about who approved, and an empty list is not an answer |
| `--push-record FILE` | repeatable, one per scope. Carries the SCM commit job into the bundle |
| `--pr` | the pull request this change came from |

A run given no `--baseline` or no `--push-record` **says so on stdout**. "Did
not look" and "found nothing" must not be indistinguishable.

See [`assessor-guide.md`](assessor-guide.md) for what the bundle asserts and how
to verify it independently.

---

## Detecting divergence

### `drift`

Has SCM diverged from what Git declares?

```sh
fwgitops drift --snapshot rules.json
fwgitops drift --state-snapshot zones.json --state-snapshot ifaces.json
```

Exit 3 means drift. Two engines, because one is not enough: the tag-based engine
finds objects carrying `gitops:` tags, and the **state** engine covers kinds that
cannot carry tags at all — `scm_zone` has no `tag` attribute, so a zone added by
hand is invisible to everything else.

### `device-sync`

Is each firewall running what SCM holds?

```sh
fwgitops device-sync
```

`drift` compares Git to SCM. This compares SCM to the **device** — the gap
between "pushed" and "live". Observed 2026-08-06: a logical router destroyed in
SCM, the push refused, and the device still forwarding on the deleted route,
with nothing reporting it.

### `snapshot`

Read a folder's live objects of one kind from SCM. Read-only; feeds `drift`.

```sh
fwgitops snapshot ZoneRequest --scope-dir prod-edge --out zones.json
```

Records the **queried** folder as `scope`, because SCM returns the folder an
object is *defined* in — without that distinction every inherited object reads
as unexpected (7 false positives against the live tenant).

### `verify-catalog`

Does `catalog/folders.yaml` still match SCM's real hierarchy?

```sh
fwgitops verify-catalog
```

Has caught two real drifts: device serials listed as child *folders*, and a
firewall that left SCM while the catalog still called it targetable. Both
produce the same failure — an intent that compiles clean and dies at apply.

Objects in SCM the catalog does not mention are **not** reported: Prisma Access
built-ins are deliberately absent, and a check that cries wolf gets ignored.

---

## Scaffolding

### `scaffold-root`

Create a Terraform root for a scope, or verify existing ones.

```sh
fwgitops scaffold-root --folder prod-edge
fwgitops scaffold-root --device 007955000901881 --device-folder prod-edge
fwgitops scaffold-root --check     # CI runs this
fwgitops scaffold-root --sync      # regenerate after a module change
```

A root must mirror the module **attribute for attribute**. A drifted root does
not fail — it quietly stops delivering part of every intent.

### `folder-interfaces`

Materialise each folder's `$`-prefixed interface variables from the catalog.

```sh
fwgitops folder-interfaces
fwgitops folder-interfaces --check
```

Run this **before compiling anything** in a greenfield folder. A zone can only
bind an interface object that exists at its scope, so these variables are what
make a folder's zones bindable at all. It reads the catalog rather than the
intent tree, because a greenfield folder has no intents yet and deriving them
from the tree would be circular.

---

## Adopting a firewall

### `adopt-device`

Point the repository at a firewall, reading SCM for every value.

```sh
fwgitops adopt-device 007955000901881 --folder prod-edge --check
fwgitops adopt-device 007955000901881 --folder prod-edge --replacing 007955000894453
```

| Flag | Effect |
|---|---|
| `--folder` | the folder SCM must **already** place the device in. Adoption refuses if SCM disagrees — writing the folder you meant would make the catalog assert a placement that is not real, and every later check trusts the catalog |
| `--replacing OLD` | rewrites the old serial across both catalogs and every device-scoped intent. A **partial** rename is the failure this exists to remove |
| `--check` | print the plan, write nothing — the same code path minus the write |

**Why it exists.** Adopting a firewall was seventeen hand edits across two
catalogs, three intents, a directory name and a Terraform root, and every one
transcribed something SCM already knew: the folder, the display name, and the
physical port behind each interface role. Those are now read.

**It closes a gap as well as saving typing.** Nothing compared
`catalog/interfaces.yaml` to the live tenant, so a wrong port there configured
the wrong interface with no error at any stage. A value read from SCM cannot
disagree with SCM — and re-running against the *same* serial is how a drifted
catalog gets corrected.

**What it refuses.** A device SCM has not placed, a device in a different folder,
and a role whose variable SCM cannot resolve — reported as unmapped rather than
guessed, because a role with no port is not a role with a default port. Exit 3 is
SCM refusing the adoption; the message says which folder it actually found.

**What it does.** Beyond the catalog and the intents: scaffolds the new device
Terraform root, removes the old one (including the gitignored files `git rm`
leaves behind), and follows the serial through `tests/` and the guides — the
files that change no behaviour and break CI anyway.

`docs/adr/` and `evidence/` are never rewritten. An ADR records a decision made
at a time; an evidence bundle records a change that really happened.

**The one thing it will not do without asking:** delete the replaced device's
Terraform state. `--prune-state` opts in. It is off by default because it is
irreversible and **remote** — the difference between editing your repository and
reaching into your cloud account to destroy a record. The bucket comes from a
root's `backend.hcl` rather than a guess.

## Device lifecycle

| Command | What it does |
|---|---|
| `fwgitops onboard <serial> --folder <f> [--name]` | verify placement in SCM and set a friendly display name. Exit 3 if onboarding failed |
| `fwgitops deregister <serial>` | remove a firewall from SCM. Exit 3 on failure |
| `fwgitops rules <folder> [--has TEXT]` | read a folder's rules from SCM. Exit 3 if a named rule is absent |
| `fwgitops set-admin-password <mgmt_ip> --ssh-key KEY` | set the device admin password over SSH. Reads the value from a prompt so it stays out of `argv` and process listings |

> A re-onboard **wipes device-scope config**. The 2026-08-05 re-onboard silently
> removed all three interface overrides while the firewall kept running its old
> config. The display name also reset to `PA-VM` — cosmetic, but a reliable
> symptom, which is why `verify-catalog` compares it.

---

## Where to look next

| | |
|---|---|
| Operating this day to day | [`operator-runbook.md`](operator-runbook.md) |
| What the evidence proves | [`assessor-guide.md`](assessor-guide.md) |
| Asking for a rule | [`requesting-rules.md`](requesting-rules.md) |
| Standing up a folder | [`building-a-folder.md`](building-a-folder.md) |
| Why the design is shaped this way | [`DESIGN.md`](DESIGN.md), [`adr/`](adr/) |
