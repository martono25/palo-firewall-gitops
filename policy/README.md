# policy — risk classifier (built in-house, Python)

No commercial firewall-analysis tool is owned (AlgoSec/Tufin/FireMon absent), so the risk
classifier is BUILT, not borrowed. Lands in Phase 2 (Phase 1 is human-approval-always).

**Python, not OPA/Rego** — the hard checks are semantic and depend on the current compiled
rulebase (shadow/redundancy, novel-zone-pair, CIDR breadth), so the classifier queries
existing SCM policy state. It shares one current-policy state model with the compiler.

Tunable thresholds (CIDR prefix cutoff, critical-rule tags) live in a YAML rule-table so
security can tune without code changes; the logic stays Python. Deterministic, versioned,
unit-tested. Every verdict emits "which rule fired + inputs" into the evidence bundle.

High-risk triggers (starting set): any-any in any field, novel zone-pair, broad CIDR/service,
deny-rule removal, ordering changes that alter existing traffic, critical-rule touches.
Thresholds are security-team decisions, not defaults.

## Built on the shared rulebase analysis core

The classifier does not re-implement shadow/redundancy analysis. It consumes the same
effective-rulebase model + 5-tuple set algebra the compiler uses for dedup (stage 5) and
placement (stage 8). See `docs/DESIGN.md` → "Rulebase Analysis Core". App-ID-aware shadowing
is a known v1 gap the classifier flags for human review (it does not claim to resolve it).

## Three-tier model + gate mapping (DECIDED)

- **LOW** → auto-merge + auto-apply after CI (CM-3)
- **HIGH** → standard approver (folder CODEOWNERS) via GitHub environment protection (CM-3, CM-5)
- **CRITICAL** → security/senior dual-control environment; never auto-eligible (AC-5, CM-3, CM-5)

Each tier targets a different GitHub Actions environment with its own required reviewers.

Two non-negotiables:
1. **Fail closed** — on error / unreadable SCM state / uncertainty, return the most restrictive
   tier. If the analysis core can't build the effective rulebase, LOW cannot be certified.
2. **Explainable + versioned** — emit fired checks + reasons into the PR comment and evidence
   bundle; record the classifier + threshold-table version that evaluated the change.

Tunable thresholds (CIDR cutoff, critical zones/assets, high-risk ports, env sensitivity, expiry
policy) live in a security-owned YAML rule-table; changes are PRs. Golden intent→tier test set
is a release gate.
