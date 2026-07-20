# BUILD STATUS — palo-firewall-gitops

_As of 2026-07-19. Phase-1 build. Full design + engineering review: [`docs/DESIGN.md`](docs/DESIGN.md)._

This repo went idea → design → engineering review → a working, tested Phase-1
compile + provision engine in one session. This document is the handoff: exactly
what is built and verified, what is scaffolded but unvalidated, and what remains.

## TL;DR

- **Verified on any machine:** the entire Day-2 compile path, the Day-1 provisioning
  orchestration, the SCM push boundary, and the SCM auth/session layer — pure Python,
  **134 passing tests**.
- **Scaffolded, marked `# VERIFY:`:** the Terraform module, the GitHub Actions
  pipeline, and the bootstrap template — structurally sound, but not runnable
  without the `scm` provider + SCM/cloud credentials.
- **Not started:** the Phase-2 analysis core, risk classifier, catalog, and drift
  detection (deliberately deferred in the review).

## Commit map

| Commit | What |
|---|---|
| `107e646` | project skeleton |
| `f21f6eb` | T6 tag/identity convention (22 tests) |
| `4f7a922` | intent schema + fail-closed loader (28 tests) |
| `f69e1b5` | compiler → rules.auto.tfvars.json (17 tests) |
| `54345b8` | CLI + YAML reader (8 tests) |
| `2dd7849` | Terraform module (UNVALIDATED) |
| `64175ad` | GitHub Actions pipeline (UNVALIDATED) |
| `732e0c1` | re-entrant provisioning orchestration (8 tests) + bootstrap template |

## ✅ Built + tested (verified)

| Area | File | Tests | Notes |
|---|---|---|---|
| Tag/identity (T6) | `src/fwgitops/tags.py` | 22 | managed marker, stable for_each keys, dedup names, fail-closed parse |
| Intent + validation (T5) | `src/fwgitops/intent.py` | 28 | uniform fail-closed contract, collects all problems |
| Env resolve | `src/fwgitops/resolve.py` | — | env → folder + zone-pair, fail-closed |
| Compiler | `src/fwgitops/compiler.py` | 17 | intent → objects + rule → byte-stable tfvars |
| CLI | `src/fwgitops/cli.py`, `io.py` | 8 | `fwgitops compile`, all-or-nothing |
| Provisioning orchestration (T3) | `src/fwgitops/provision.py` | 8 | re-entrant, license retry, bounded poll |
| SCM push / commit boundary (T13) | `src/fwgitops/push.py` | 10 | fail-closed guard, folder-scoped push, bounded job poll |
| SCM auth/session | `src/fwgitops/scmapi.py` | 17 | VERIFIED flow: basic auth → client_credentials + `tsg_id:` scope → JWT, cached w/ early refresh |

Run it:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest -q                                              # 134 tests
fwgitops compile intent --check                        # validate-only
fwgitops compile intent --out terraform                # emit rules.auto.tfvars.json
```

## ⚠️ Scaffolded, UNVALIDATED (needs your environment)

Every assumption is marked `# VERIFY:`. Resolve before first `apply`.

| Area | Path | Blocking dependency |
|---|---|---|
| ~~Terraform module~~ | `terraform/modules/security_folder/` | ✅ **SCHEMA-VERIFIED** against scm v1.0.11 — `terraform validate` passes (Part A done) |
| Per-folder root | `terraform/prod-edge/` | remote backend (Arch-2), provider auth (T1) |
| CI pipeline | `.github/workflows/{pr-validate,apply}.yml` | T1 auth, Arch-2 backend, `firewall-apply` environment |
| Review gate | `.github/CODEOWNERS` | real team handles |
| Bootstrap | `provisioning/bootstrap/init-cfg.sample.txt` | SCM onboarding keys |
| Cloud instantiate | (pointers) | use Palo's `terraform-aws/google-vmseries-modules`, not blind HCL |
| **SCM REST clients** | `src/fwgitops/clients.py` | ⚠️ endpoint paths/payloads are UNVERIFIED guesses (`# VERIFY:`); auth layer beneath them IS verified. Parsing tolerance + fail-safe defaults are tested. |

The Python↔Terraform contract (`rules.auto.tfvars.json` shape) **is** verified end-to-end:
compiler output type-checks through Terraform's variable types against the real provider
schema (a `plan` reaches provider auth — `ClientId must be specified` — with no type errors).

### Part-A spike results (2026-07-19) — provider `PaloAltoNetworks/scm` v1.0.11

Four assumptions were wrong and are now fixed: `scm_address_object`→**`scm_address`**,
`scm_security_policy_rule`→**`scm_security_rule`**, `tags`→**`tag`** (singular,
`list(string)`), and `protocol` is a **nested attribute** (`protocol = { tcp = { port } }`)
not a block. Provider pin corrected `~> 0.9`→**`~> 1.0`**. Tags are free-form strings at the
Terraform layer, so `fwgitops.tags` needed no change. Provider does the OAuth
client-credentials→JWT exchange itself (feeds T1). Schema dump: `spike/schema.json`.

### Part-B results (2026-07-19) — live apply against the `GitOps` lab folder ✅ SPIKE COMPLETE

Module verified end-to-end against a real tenant (tag + address + service + rule created).
Four more findings, all fixed or recorded:

- **Tags must pre-exist as `scm_tag` objects** — SCM validates them as references
  (`INVALID_REFERENCE`). The module now creates them and orders tags → objects → rules.
- **apply requires `-parallelism=1`** — the provider cannot handle concurrent token
  acquisition; it fails with a misleading `unauthorized_client`. Baked into `apply.yml`.
- **No push/commit in the provider** (0/129 resources) — `apply` only *stages* config; a
  separate push (**target = folder**) makes it live. The candidate/commit boundary is
  structurally forced, not just a design preference.
- **Folder-scoped push** — a push commits everything staged in the folder, so applies must be
  serialized per folder and the push must **fail closed** on unexpected staged changes
  (that delta is Level-1 drift).
- Folder ownership stays with Day-1 (`scm_folder`); the Day-2 module must never own it.

Full write-up: `docs/SPIKE-scm.md` → RESULTS.

**T13 (SCM push):** orchestration + fail-closed guard are BUILT and TESTED
(`src/fwgitops/push.py`, 10 tests). Remaining: the thin `PushClient` implementation against
the SCM REST API (`list_staged` / `push` / `job_status`) — needs tenant access to verify.

## ⬜ Not started (Phase 2 / 3 — deferred by review)

T4 state model · risk classifier · T7 cache · T8 catalog-from-IPAM · T10 Level-2
drift · T11 expiry job · T12 window scheduler. See `docs/DESIGN.md` → Implementation Tasks.

## Handoff: first moves in an environment with SCM access

1. **`scm` provider spike** — burn down `terraform/modules/security_folder/README.md`
   with `terraform providers schema -json`. This is the #1 de-risker.
2. **Fill T1 auth + Arch-2 backend** — short-lived SCM token, remote state per folder.
3. **Implement `ProvisionClient`** (`src/fwgitops/provision.py`) against the SCM REST API —
   the orchestration around it is already tested.
4. **Pilot** — pick AWS or GCP + a greenfield SCM folder; provision one VM-Series
   end-to-end; run one intent through the pipeline.

## Design decisions (reference)

All load-bearing decisions and their rationale are in `docs/DESIGN.md`
(Engineering Review → Decisions made in review). Key ones baked into the code:
fail-closed everywhere · stable content-derived for_each keys · 1:1 per-commit
isolation · objects-before-rules · re-entrant provisioning · human-approval-always
in Phase 1 (classifier is Phase 2).
