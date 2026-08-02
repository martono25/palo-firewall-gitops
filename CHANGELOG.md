# Changelog

All notable changes to `fwgitops` are documented here. This project follows
[Semantic Versioning](https://semver.org/).

## [1.7.0] — 2026-08-02

Security hardening of the CI path. No functional change.

### Credentials can no longer reach a published artifact or PR comment
`pr-validate` folds terraform's stderr into `plan-*.txt`, uploads it as an
artifact, and pastes it into a PR comment — while `SCM_CLIENT_SECRET` sits in the
job env. **GitHub masks the live log stream but not artifact contents or
`gh pr comment` bodies**, so a provider or auth error echoing a credential would
land somewhere durable and public while the visible log looked clean.

Not observed — structural, and open since the plan-step fix in v1.1.0 introduced
the `2>&1`.

`.github/scripts/redact.py` strips secret values before either publish step, with
`if: always()` because a failing plan is exactly when such an error is most
likely. Literal substring replacement, not regex: a secret can contain any
character.

One test asserts **every secret the workflow injects** appears in `SECRET_VARS`,
so adding a secret to the job env without redacting it fails the suite.

### `spike/zone-probe` refuses production folders
It carried the warning in prose only, while `interface-probe` enforced it in a
`validation` block. Prose is not a guard. Now refuses `prod-edge`,
`ngfw-shared` and `All`; verified all three rejected and `GitOps` accepted.

**Tests: 458 → 470.**

## [1.6.0] — 2026-08-02

**ADR-0001's registry promise, finally kept.** Adding an intent kind is now one
registry entry instead of ~8 hand-edited sites.

### `fwgitops.kinds.REGISTRY`
Each kind registers a `KindHandler` carrying its compile function, tfvars
filename and emitter, folder/name accessors and classifier. Replaces:

- `compile_any`'s isinstance chain (moved out of `compiler.py`, which now owns
  the per-kind compile *functions* while the registry owns choosing between them)
- **eleven** isinstance filters in `cli.py`
- a hand-written tfvars emission block per kind — now one loop over the registry

If a kind's Terraform side is missing, the compile fails closed (ADR-0004)
instead of emitting data nothing reads. That is the failure `ZoneRequest` shipped
with for an entire release.

### What it deliberately does NOT unify
A protocol with optional members for stages a kind cannot support would be an
interface with holes. Two stages are genuinely not uniform, so capability is
**declared**:

| Field | Why |
|---|---|
| `drift_engine` | `"tag"` for rules (they carry `gitops:` provenance), `"state"` for zones (`scm_zone` has no `tag` attribute). Same word, different mechanism. |
| `has_evidence` | `build_bundle` is rule-shaped; there is no kind-agnostic bundle today. |

### Tests
New `tests/test_kinds.py` asserts the registry is **complete and
self-consistent**, not merely that dispatch works: every handler fully populated,
kinds matching the intent loaders exactly, tfvars filenames unique, every
filename covered by the gitignore glob, compiled types distinct and
non-overlapping. Verified by mutation — registering a kind under the wrong name
fails five of them.

**Tests: 442 → 458.**

## [1.5.0] — 2026-08-02

ADR-0005's prerequisite 2, generalised beyond interfaces and shipped exercised
on a kind that exists today.

### New check — `zone_becomes_traffic_bearing` (HIGH)
Populating a previously-empty security-relevant field is **not the same act** as
editing a populated one. Assigning an IP to an unaddressed interface puts it on a
network; binding an interface to an empty zone starts carrying traffic through
it. Editing either changes something already live.

This is not hypothetical: **four of the seven zones on the pilot tenant sit at
`layer3: []`** — the normal state, not an edge case. A change moving one out of
it alters what the firewall passes, and now will not auto-apply at a LOW gate.

`_becomes_populated` is the shared helper; interface addressing plugs into it
when `InterfaceRequest` lands.

### `fwgitops classify --zones-snapshot`
State-aware checks need current state, so `classify` accepts the snapshot
produced by `snapshot-zones`. Absent snapshot **disables** those checks rather
than guessing — the classifier says what it can prove.

Keys on the snapshot's `scope` (the folder QUERIED), not the folder SCM reports
an object as defined in. Getting that backwards would mean an inherited zone
never matches its declaration and the check silently never fires.

**Tests: 434 → 441.**

## [1.4.0] — 2026-08-02

Two of ADR-0005's four blocking prerequisites for `InterfaceRequest`. Both close
gaps that exist today rather than only mattering later.

### HOLE 3 now applies at any depth
The object-attribute check inspected only the TOP level of an `object({...})`
type — a documented limitation. Terraform discards an undeclared attribute at
**any** depth, and both `network` (zones) and `layer3` (interfaces) are nested,
so a root whose nested type was narrower than the module's would drop fields
while the top-level key looked perfectly wired.

The check now recurses and compares dotted paths
(`network.zone_protection_profile`). A `null` nested object asserts nothing about
its children, so an unset `optional(object(...))` is not a false positive.

### New check — `folder_with_children` (HIGH)
A change scoped to a folder that has child folders reaches every one of them. On
this tenant `ngfw-shared` parents both `prod-edge` (production) and `GitOps`
(sandbox), so one change there lands on both — the largest blast radius this
platform can produce.

Driven by a new `catalog/folders.yaml`. The classifier stays **pure**: the
hierarchy is declared config, not a live SCM read. Applies to every kind, so an
env map pointing at a parent folder tiers up its *rules* too, not just
interfaces. Absent hierarchy disables the check rather than inventing a verdict.

**Tests: 421 → 434.**

## [1.3.0] — 2026-08-02

**Drift detection now covers objects that cannot carry tags.**

The existing engine keys entirely off `gitops:` tags. That works for security
rules and covers nothing else: `scm_zone` and `scm_ethernet_interface` have no
`tag` attribute, and only **14** of the provider's resources do. Zones — shipped
in v1.2.0 — were invisible to drift detection, in a product whose deliverable is
NIST-mapped compliance evidence.

### New — state-based drift
Without a provenance marker you cannot ask "did *we* create this?". You can still
ask what matters:

| Class | Meaning |
|---|---|
| `UNEXPECTED` | Present in SCM, neither declared nor a known baseline object |
| `MISSING` | Declared in Git, absent from SCM |
| `MODIFIED` | Declared and present, but a field differs |

The `baseline_zones` allowlist (v1.1.0) is what makes `UNEXPECTED` meaningful
rather than noise — it names the objects that legitimately pre-date GitOps.

Only fields the declaration actually **sets** are compared: a `null` means "we
did not ask for this", so SCM's value is not drift. Desired state is built from
the compiler's own tfvars emitter, so drift and what Terraform applies cannot
disagree about what a zone should look like.

### New — `fwgitops snapshot-zones`
Read-only SCM read producing the snapshot, wired into `drift-detect.yml`. This is
the only check that can see a zone **added by hand**: `terraform plan` sees only
changes to resources already in its state, and zones carry no tags.

### Inheritance
SCM returns the folder an object is **defined in**, not the folder queried. Every
zone on the pilot tenant is defined in the shared parent, so keying on the
returned folder reported all seven as unexpected. Inherited objects are platform
config the child folder does not own — they are counted and reported as context,
never as drift. Found by running against the live tenant, not by reasoning.

### Known limit
`UNEXPECTED` cannot distinguish an orphan ("we made it, the intent was deleted")
from an unmanaged object ("someone made it by hand"). The tag-based engine can,
because a rule carries its own provenance. Here there is nothing to read, so both
collapse into one class and the report does not claim to know the cause.

**Tests: 408 → 421.**

## [1.2.0] — 2026-08-02

**`ZoneRequest` reaches the firewall.** Kind #2 has existed since #18 but never
had a Terraform resource behind it — v1.1.0 made that failure loud instead of
silent; this closes it. Zones now carry a full security posture, not just a name
and an interface list.

### Zones reach the device
- `scm_zone` resource + `zones` variable + module wiring. The root and module
  object types are byte-identical and the compile-time contract check
  (ADR-0004) enforces that per-attribute, so HOLE 3 cannot recur here.
- Rules **order after the zones they reference**: a rule's `from`/`to` resolves
  through `scm_zone.this[...]` for zones this module manages, while baseline
  zones (`local`, `internet`, `proxy`) pass through as plain strings. The
  predicate reads `var.zones`, not the resource, so the branch stays decidable
  at plan time — and it is deliberately not a blanket `depends_on`, which
  previously caused a destroy-cascade on address objects.

### Zone security posture (the ADR-0003 lesson, applied to zones)
A zone is not just a name and a port list. `ZoneRequest` now accepts:

| Field | Why it matters |
|---|---|
| `protection_profile` | Absent = **no** flood, reconnaissance or packet-based-attack protection |
| `user_id` | Off = any rule matching `source_user` **silently never matches** |
| `log_forwarding` | Otherwise local logs only |
| `device_id`, `dos_profile`, `dos_log_forwarding`, `user_acl`, `device_acl` | Full provider parity |

`protection_profile` is a **zone**-protection profile — flood/recon, bound to a
zone. Not the same thing as a rule's `profile`, which is a security profile
*group* giving IPS/AV/URL inspection. A zone with neither has neither. New
catalog: `catalog/zone-protection.yaml`.

### Risk classification for zones
`fwgitops classify` covers zones, having previously dropped them on the floor
("policy stages: rules only"):

- `zone_without_protection` (**HIGH**) — the `allow_without_inspection` lesson
  for zones: the absence of a security control is a finding, not a default.
  A LOW gate now refuses to auto-apply an unprotected zone.
- `user_id_disabled_on_zone` (LOW note) — the rule model has supported
  `source_user` since v1.0, and the failure is silent: the rule is skipped and
  traffic falls through to whatever is next.

### Fixed
`_load_zone_request` built its collector **without catalogs**, so a ZoneRequest's
reference names were never validated at all. A typo'd profile now fails at PR
time, as ADR-0003 requires for rules.

### Known limits
Zones cannot be drift-tracked: `scm_zone` has no `tag` attribute, so the
`gitops:` provenance markers `drift.py` relies on cannot be attached. Only 14 of
the provider's resources are taggable — this is a general limit of the
tag-based model, not a zone quirk. Tracked in `TODOS.md`.

**Tests: 392 → 408.**

## [1.1.0] — 2026-08-02

Closes a class of bug where the compiler produced config that **silently never
reached the firewall** while every check stayed green. Four distinct instances
were found and fixed; three of them were invisible to the 327-test v1.0 suite by
construction, because every test asserted the compiler wrote the right JSON and
stopped exactly where the failure began.

### ⚠ Behaviour change
`fwgitops compile` now **rejects** (exit 2, nothing written) when it would emit
into a folder that has no Terraform root. This previously succeeded silently.
Pass `--allow-missing-root` if you genuinely mean a scratch or scaffold
directory.

### The silent-drop holes

| # | Hole | Terraform's signal |
|---|---|---|
| 1 | tfvars key with no matching `variable` | warning, **exit 0** |
| 2 | `variable` declared but never referenced | **no diagnostic at all** |
| 3 | object attribute the target type omits | **silently discarded** |

**Hole 1** shipped for a full release: `zones.auto.tfvars.json` was written on
every compile while `terraform/prod-edge` declared no `zones` variable and the
module had no `scm_zone` resource. Compile, plan, apply and CI all green; the
zone never reached the device.

**Hole 3** was live in v1.0. The root module's `security_rules` type omitted the
six ADR-0003 attributes the module declares and the compiler emits —
`application`, `profile_group`, `log_setting`, `rulebase`, `relative_position`,
`target_rule` — so the module received its own defaults instead of the intent's
App-ID and profile. Root and module types are now identical.

### New — `fwgitops.tfcontract`
Checks holes 1 and 2 in pure Python, with no Terraform binary and no cloud
credentials, at compile time (fail-closed) and in CI. Hole 3 needs a
schema-level check; tracked in `TODOS.md`.

The parser is string-literal aware, which is load-bearing: a `}` inside a string
used to collapse brace depth and let an unwired variable pass, and a `//` inside
a URL was read as a comment and falsely rejected a valid module. Line breaks are
never masked — dropping one desynced the comment pass and truncated
`module "n" {` to `mo`.

### Fixed — CI guards that were not guarding
- `pr-validate` piped `terraform plan` through `tee` and appended `|| true`, so
  **every** plan failure was swallowed and `-detailed-exitcode` was meaningless.
  Exit 2 (changes present) is correctly treated as normal for a PR.
- `apply.yml` had no undeclared-variable check at all — the backstop guarded the
  preview but not the path that touches the device.
- Both workflows now fail when a folder has emitted tfvars but no Terraform root,
  instead of `continue`-ing past it.
- Plan artifacts upload with `if: always()`, so they survive the failure that
  makes them worth reading.

### Fixed — zone handling
- `catalog/environments.yaml` gains an optional `baseline_zones` list. It
  declared two baseline zones while the folder carries seven, so a rule
  referencing a real zone such as `proxy` was **rejected at compile time as
  undeclared** — fail-closed machinery producing a false negative.
- A `ZoneRequest` naming a zone that already exists on the device is rejected.
  The consistency check unions baseline and declared zones, so such a request
  looked maximally valid while Terraform would have created over a live zone.
- A valueless `baseline_zones:` key no longer errors — commenting out the list
  under its comment block is the natural edit and it used to brick compile.

### Security
`ScmCredentials.client_secret` is `repr=False`; the dataclass `__repr__`
rendered the live tenant secret in cleartext.

### Verified live (provider v1.0.11)
`scm_zone` writes its fields **faithfully** — the computed-attribute drop that
breaks `scm_security_rule` (ADR-0003) does not apply, so zones need no `enrich`
workaround. SCM also reference-validates zone fields fail-closed. Probe
committed at `spike/zone-probe/`; fidelity varies per resource type, so re-run it
before scoping `InterfaceRequest`.

**Tests: 327 → 382.** See [ADR-0004](docs/adr/0004-compiler-terraform-contract.md).

## [1.0.0] — 2026-07-29

First production release. **Day-2 security-rule provisioning is complete and
proven end-to-end on live hardware** (VM-Series, PAN-OS 11.2.12): a rule intent
flows `intent → compile → classify → risk-gate → terraform apply → enrich → push`
and lands in the firewall's running config, verified on-device via SSH.

### Rule model — full standard-policy expressiveness
A compiled rule now carries the complete set of common security-rule fields:
- **Match:** zones, source/destination addresses, **service**, **App-ID**
  (`application`), **User-ID** (`source_user`), **URL category** (`category`),
  and **negation** (`negate_source` / `negate_destination`).
- **Action:** `allow` / `deny` / `drop` / `reset-client` / `reset-server` /
  `reset-both`.
- **Inspection & logging:** security **profile group** (`profile`), external
  **log-forwarding** (`log_forwarding`), session **log_start** / **log_end**.
- **Placement:** rulebase + ordering (`top` / `bottom` / `before:<rule>` /
  `after:<rule>`).
- **Metadata:** `description`, provenance tags.

### The `enrich` step (provider-gap workaround)
The `paloaltonetworks/scm` Terraform provider silently drops `application`,
`profile_setting`, `log_setting`, ordering, and other rule fields (v1.0.11 and
v1.0.12-beta.3; see `docs/scm-provider-securityrule-bug.md`). `fwgitops enrich`
sets them via the SCM REST API after `terraform apply` and before `push`, so the
push commits skeleton + enrichment as one atomic change. Terraform owns the rule
skeleton + state/drift/rollback; enrich owns the fields the provider can't write.

### Safety & governance
- **Fail-closed everywhere:** invalid intent, unresolvable object, or unclassifiable
  change never produces a partial result.
- **Risk classifier** with a fail-closed tier gate (LOW auto / HIGH / CRITICAL);
  checks include broad match, any-any, internet exposure, novel zone-pair,
  shadowing, `allow_without_inspection`, and `negated_match`.
- **Name catalogs** validate App-ID / profile / log-forwarding names at PR time.
- **NIST-mapped evidence bundle** per change (records the full effective rule).
- **CI:** `pr-validate` runs tests + compile + classify + enrich preview + plan;
  `apply` auto-triggers on merge (fail-closed at LOW).

### Deferred to a later release
Day-1 provisioning (interfaces / IP / zones / virtual router), `schedule`, HIP
(`source_hip` / `destination_hip`), `policy_type: Internet`, and
`tenant_restrictions`.
