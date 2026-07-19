# terraform — Day-2 reconcile state (data-driven, for_each)

Declarative desired-state for Day-2 policy via the `scm` provider. `plan` is the PR preview and
drift detector; `git revert` + apply is rollback. State split per SCM folder — never one
monolithic state.

**Output contract:** the compiler emits `rules.auto.tfvars.json` per folder; a hand-written
STATIC module here iterates with `for_each`. The module is authored + reviewed once; only the
JSON data changes per request. `for_each` keys MUST be stable (REQ-id / deterministic hash) to
avoid rule churn/recreation.

Do NOT bulk-import the brownfield rulebase; onboard scope greenfield-first (Premise 4).
