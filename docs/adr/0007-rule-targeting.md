# ADR-0007 — An `AccessRequest` targets an environment, never a folder or a firewall

- **Status:** Accepted (2026-08-05)
- **Date:** 2026-08-05
- **Deciders:** Martono, Claude

## Context

ADR-0006 gave the Day-1 kinds (`InterfaceRequest`, `ZoneRequest`, `RouteRequest`)
explicit `folder:` / `device:` targeting, on the grounds that their author is a
network engineer for whom the target IS the intent. `AccessRequest` kept
`environment:` only.

Part of the justification at the time was believed to be a constraint: that SCM
would not accept a security rule at device scope. **That belief was wrong.**
Three spikes concluded it while the pilot firewall was in a broken registration
state; after it was offboarded and re-onboarded, every resource — zone, logical
router, address, tag and security rule — accepted a device-scope write.

So the exclusion had to be re-decided on its merits rather than inherited.

Two things also changed the economics:

* folder creation became cheap and safe — `scaffold-root`, `folder-interfaces`,
  `verify-catalog`, and a warning when a folder has no firewall beneath it;
* the `environment` indirection was tested by accident, twice in one week. A
  firewall left SCM, and the pilot was offboarded and re-onboarded with a new
  object id and its device-scope config wiped. **Not one `AccessRequest`
  changed.** The churn was absorbed by `catalog/folders.yaml` and an apply.

## Decision

**`AccessRequest` targets `environment:` only.** `folder:` and `device:` are
rejected at PR time with a message that states the reason, not a generic
"unknown field" — for a targeting field, *why* is the whole question.

The line that decides it:

> **Device scope is for CONFIGURATION. The unit of POLICY is the folder.**

An interface address is genuinely per-firewall — two firewalls cannot share
`10.100.2.142/24`, so `InterfaceRequest` must be able to name a device. A
security rule is not like that. A rule that applies to one firewall and not its
neighbours is a policy *override*, and per-firewall policy divergence is
something an operator has to reason about for as long as it exists.

If a rule genuinely must apply to one firewall, that firewall needs its own
folder and environment. That is a deliberate, visible act with a blast radius you
can read off the catalog — not a field on a change request.

## Consequences

**An app team cannot place a rule.** They say what access is needed; the platform
decides where it lands. This is the property that absorbed both topology events
above, and the reason to keep it is empirical rather than aesthetic.

**Per-firewall policy costs a folder.** Accepted. It is now a PR (`scaffold-root`
+ a catalog entry), the empty-folder warning catches the half-finished state, and
the cost is proportionate to what is being asked for: a firewall whose policy
diverges from its fleet.

**`folder:` is NOT added for platform-authored rules — yet.** It is a plausible
future need, and adding it before there is a real one would be speculative
surface. This repo has deleted three fields in a week that were declared, stored
and never read (`app.folder`, `metadata.expires`, `devices.hostname`); each
looked authoritative and did nothing. If platform rule placement becomes a real
requirement, add it then, with the case that motivated it.

**This ADR is the thing to argue with.** The exclusion is enforced by
`_ACCESS_SPEC_KEYS` plus an explicit rejection carrying the reasoning, and a test
pins both. Re-enabling either field means changing a test whose docstring says
why it exists, which is the intended amount of friction.

## Alternatives considered

**Add `folder:` for platform-authored rules, keep `environment:` for app teams.**
Coherent, and the natural extension of ADR-0006. Rejected only on timing: there
is no current requirement, and two ways to say the same thing for the common case
(`environment: prod` == `folder: prod-edge`) is ambiguity in the one field that
must never be ambiguous.

**Add `device:` as well, since SCM permits it.** Rejected on the principle above.
Availability is not a reason: the capability exists for configuration, and using
it for policy buys per-firewall exceptions at the cost of a fleet that is no
longer uniform by construction.

**Fold placement into the app catalog (Model B).** Considered and rejected
separately — a rule's folder is a property of the traffic path, not of either
endpoint, so a rule between apps in two folders has no unambiguous answer.
