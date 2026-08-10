# `AUTOMATION_PR_TOKEN` — why the pipeline needs a token that isn't the bot's

**Audience: whoever administers this repository.** One secret, created once,
rotated when it expires.

---

## The problem it solves

Two workflows open pull requests: `apply.yml` (evidence bundles) and
`intake.yml` (a requester's generated intent). Both used the default
`GITHUB_TOKEN`, and GitHub documents what happens then:

> Events triggered by the `GITHUB_TOKEN` will not create a new workflow run,
> with the following exceptions … `pull_request` events with `opened`,
> `synchronize`, or `reopened` activity types create runs in an
> **approval-required** state.

`main` requires `pytest` and `compile-and-plan` to pass. Those checks never
start on a bot-opened PR, so the PR can never merge until a human opens the run
and approves it.

**No repository setting lifts this.** It is deliberate recursion protection, not
a policy knob — loosening `fork-pr-contributor-approval` does nothing, and the
bot never ages out of it: three of its PRs had already merged when the fourth
was still held.

It was hit four times on 2026-08-10 (#123, #134, #137, #140). For evidence it
means an audit record that waits on a click — the artifact-with-a-TTL problem in
another form. For intake it means a requester's change sitting there looking
unvalidated.

## What to create

A **fine-grained personal access token**, scoped to this repository only:

| | |
|---|---|
| Repository access | Only select repositories → `palo-firewall-gitops` |
| Contents | Read and write |
| Pull requests | Read and write |
| Expiration | Your call; **90 days is the sensible ceiling** |

Nothing else. **In particular it does not need `Issues`** — the workflows comment
on issues with the run's own default token, so the long-lived credential is never
scoped to write them. Adding `Issues` to save one variable would widen a
credential that outlives every run that uses it.

Then store it:

```sh
gh secret set AUTOMATION_PR_TOKEN
```

Paste when prompted. Do not put it on the command line — the shell records that.

## What it does NOT change

- **It does not bypass the ruleset.** PRs still need `pytest` and
  `compile-and-plan` to pass; `main` still takes no direct push.
- **It does not approve anything.** The `firewall-apply` environment reviewer is
  untouched — a HIGH or CRITICAL change still waits for a human.
- **It is not used for the apply itself.** SCM credentials are separate secrets.
- **It does not comment.** Rejections and "opened your PR" notices go out under
  the default token, which holds `issues: write` only for the duration of a run.

Its whole job is authorship: a PR opened by a user account gets its checks run,
which a PR opened by the bot does not.

## When it expires

The pipeline keeps working and degrades to the old behaviour — PRs open, checks
wait for a manual approval. **The run says so**, both in `apply.yml` and
`intake.yml`:

```
::warning::AUTOMATION_PR_TOKEN is not set — github-actions[bot] opens this PR …
```

That warning is deliberate. A secret that expires is a certainty rather than a
risk, and the failure it causes is quiet: everything still runs green, records
just stop landing. A test asserts the warning exists, so it cannot be tidied
away.

## If you would rather not hold a credential

Three alternatives were considered and are recorded in `CHANGELOG.md`:
dispatching the checks against the evidence branch (`workflow_dispatch` always
runs, but it is unproven whether a dispatched run satisfies a required check on
the PR head SHA), having the apply job approve its own runs (a workflow
approving the gate on its own output), and keeping evidence off `main` entirely
on an unprotected branch (which costs `git log evidence/<scope>/<REQ>.json`
being that request's whole life).
