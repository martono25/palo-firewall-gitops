# Architecture Decision Records

Focused records of significant, hard-to-reverse design decisions. Each captures
the context, the decision, and its consequences at a point in time.

| ADR | Title | Status |
|---|---|---|
| [0001](0001-multi-kind-intent-model.md) | Multi-kind intent model | Proposed |
| [0002](0002-day1-provisioning-thin-bootstrap.md) | Day-1 provisioning: thin bootstrap + ordered config jobs | Proposed |
| [0003](0003-security-rule-component-model.md) | Security-rule component model (App-ID, profiles, forwarding, ordering) | Accepted |

0001 and 0002 are accepted *directions* for a later phase, not yet built (0002
depends on 0001). 0003 is **built and tested** (live apply pending the pilot VM).
See `docs/DESIGN.md` for the overall Phase 1 → Phase 2 design and roadmap.
