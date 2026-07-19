# BUILD STATUS — palo-firewall-gitops

_As of 2026-07-19. Phase-1 build. Full design + engineering review: [`docs/DESIGN.md`](docs/DESIGN.md)._

This repo went idea → design → engineering review → a working, tested Phase-1
compile + provision engine in one session. This document is the handoff: exactly
what is built and verified, what is scaffolded but unvalidated, and what remains.

## TL;DR

- **Verified on any machine:** the entire Day-2 compile path and the Day-1
  provisioning orchestration — pure Python, **83 passing tests**, no live access needed.
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

Run it:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest -q                                              # 83 tests
fwgitops compile intent --check                        # validate-only
fwgitops compile intent --out terraform                # emit rules.auto.tfvars.json
```

## ⚠️ Scaffolded, UNVALIDATED (needs your environment)

Every assumption is marked `# VERIFY:`. Resolve before first `apply`.

| Area | Path | Blocking dependency |
|---|---|---|
| Terraform module | `terraform/modules/security_folder/` | **`scm` provider spike** — the module README is the checklist |
| Per-folder root | `terraform/prod-edge/` | remote backend (Arch-2), provider auth (T1) |
| CI pipeline | `.github/workflows/{pr-validate,apply}.yml` | T1 auth, Arch-2 backend, `firewall-apply` environment |
| Review gate | `.github/CODEOWNERS` | real team handles |
| Bootstrap | `provisioning/bootstrap/init-cfg.sample.txt` | SCM onboarding keys |
| Cloud instantiate | (pointers) | use Palo's `terraform-aws/google-vmseries-modules`, not blind HCL |

The Python↔Terraform contract (`rules.auto.tfvars.json` shape) **is** verified —
it is produced and consumed on both sides. Only the provider mapping is unverified.

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
