# Changelog

All notable changes to `fwgitops` are documented here. This project follows
[Semantic Versioning](https://semver.org/).

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
