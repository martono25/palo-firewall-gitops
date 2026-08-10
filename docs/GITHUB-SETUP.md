# GitHub setup

The GitHub side of the pipeline: what enforces what, and what you have to set up
once. Repo: **github.com/martono25/palo-firewall-gitops** (public).

---

## What actually blocks a change

Four controls, all live and all verified against the running repo rather than
inferred from settings pages.

| Control | Mechanism | Verified |
|---|---|---|
| No direct write to `main` | repository ruleset, **no bypass actors** | push rejected 2026-08-10 |
| Every change reviewed by CI | required checks `pytest` + `compile-and-plan` | every PR since #118 |
| HIGH / CRITICAL apply held for a human | `firewall-apply` environment, required reviewer | held twice 2026-08-10 |
| LOW applies without a human | `firewall-apply-auto` environment, no reviewer | applied 2026-08-10 |

Which environment a run uses is chosen by the risk tier in the `classify` job —
not by an input anyone fills in. See `apply.yml`.

### The ruleset

```bash
gh api repos/martono25/palo-firewall-gitops/rulesets --jq '.[].name'
```

`bypass_actors` is deliberately empty, and `current_user_can_bypass` reads
`never`. That includes the pipeline: `apply.yml` opens a **pull request** for its
evidence bundles rather than pushing them, because a workflow exempt from the
rule it enforces is not enforcing it.

On a user-owned repository the `github-actions` app **cannot** be a bypass actor
at all — `422: must be part of the ruleset source or owner organization` — so
there was never a version of this where the workflow was excepted.

Test it in the direction that should fail, which is the only direction that
proves anything:

```bash
git commit --allow-empty -m "probe: must be rejected"
git push origin main     # expect: "Changes must be made through a pull request."
git reset --hard origin/main
```

### Why this needed a public repo

On the **free** plan a **private** repo cannot enforce branch protection,
rulesets, or environment required reviewers (`403: Upgrade to GitHub Pro or make
this repository public`). Verified 2026-07-23, and for three weeks the gate was
configured but not enforced. Making the repo public is what turned the
approval model from a diagram into a control.

That trade is worth naming: **everything in this repository is world-readable**,
including intent files, evidence bundles and Terraform roots. Nothing here may
contain a credential, an internal hostname you would not publish, or a real
customer address. See "Secrets" below — that rule has already been broken once.

---

## Secrets

`gh secret set` reads the value from your input and stores it encrypted. Run each
and paste at the prompt. **Do not put a value on the command line** — the shell
records it.

```bash
gh secret set SCM_CLIENT_ID       # the service-account identity
gh secret set SCM_CLIENT_SECRET   # the service-account secret
gh secret set SCM_SCOPE           # tsg_id:<your tenant id>
gh secret set AUTOMATION_PR_TOKEN # see docs/automation-token.md
```

Names only, never values:

```bash
gh secret list
```

`.github/scripts/redact.py` strips `SCM_CLIENT_ID`, `SCM_CLIENT_SECRET` and
`SCM_SCOPE` from anything the CI publishes — artifacts and PR comments, which
GitHub does **not** mask even though it masks the live log stream. Only the
secret is a credential; the other two are stripped because a published artifact
is not the place for account identifiers either.

> **Use placeholders in every example, always.** Until 2026-08-10 these docs
> printed this deployment's own service-account identity and tenant id beside
> `gh secret set`. Neither is a credential — authentication needs
> `SCM_CLIENT_SECRET`, which was never exposed — so this was not an incident.
> It was two smaller things worth fixing anyway: **the examples only worked for
> one tenant**, which makes the runbook useless to anyone else, and the repo
> asserted both positions at once, with `redact.py` stripping values the docs
> printed.
>
> The position is now stated once: the identity and tenant id are account
> identifiers, not secrets. `redact.py` still strips them from published CI
> artifacts as defence in depth, which costs nothing, and the docs use
> placeholders so they are portable.

### Cloud credentials

The Terraform state backend authenticates by **OIDC**, not long-lived keys:
`vars.AWS_OIDC_ROLE_ARN`, assumed per run. Nothing to rotate.

---

## `CODEOWNERS`

`.github/CODEOWNERS` is `@martono25`. With one collaborator it documents intent
rather than enforcing anything — the ruleset requires zero approving reviews,
because requiring one from the only human who can give it would deadlock every
change.

**This is the honest limit of the current model.** `AC-5` (separation of duties)
is not earned: the same person authors, approves and releases. The evidence
bundle records `via: deployment_gate` versus `via: pull_request_review` precisely
so that one person doing both is visible as a finding rather than hidden as a
detail. Add a second collaborator and raise the review count to make it real.

---

## What is deliberately not enforced

- **CRITICAL is not dual-controlled.** It routes to the same reviewed
  environment as HIGH. Genuine dual control needs a second human.
- **`enforce_admins` has no equivalent to switch off.** The ruleset has no
  bypass actors, so admins are already bound by it. That is the intent.
