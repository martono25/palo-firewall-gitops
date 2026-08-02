# Architecture Decision Records

Focused records of significant, hard-to-reverse design decisions. Each captures
the context, the decision, and its consequences at a point in time.

| ADR | Title | Status |
|---|---|---|
| [0001](0001-multi-kind-intent-model.md) | Multi-kind intent model | Accepted — partially built |
| [0002](0002-day1-provisioning-thin-bootstrap.md) | Day-1 provisioning: thin bootstrap + ordered config jobs | Accepted — bootstrap half built |
| [0003](0003-security-rule-component-model.md) | Security-rule component model (App-ID, profiles, forwarding, ordering) | Accepted — built, proven on hardware |
| [0004](0004-compiler-terraform-contract.md) | The compiler → Terraform contract must be enforced | Accepted — built |
| [0005](0005-interfacerequest-folder-scope.md) | `InterfaceRequest` targets folder scope via the `$eth-*` variables | Accepted — direction |

**0001** — the intent-loader registry and `ZoneRequest` are built; the registry
is otherwise a registry in name only (classify / evidence / drift are still
hard-typed to security rules). Its stage table originally claimed those stages
were kind-agnostic; that claim is corrected in the ADR itself.

**0002** — the bootstrap half is built and proven on live hardware. The
data-plane half (`InterfaceRequest`, `RouteRequest`, `NatRequest`, cross-kind
ordering) is not. `InterfaceRequest` is the prerequisite for a zone that can
carry traffic.

**0003** — built, tested, and proven end-to-end on a live VM-Series.

**0004** — the fail-closed contract that stops a kind being wired into the
compiler but not into Terraform. Also records the live probe showing `scm_zone`
does *not* suffer the provider defect that 0003 works around.

**0005** — corrects 0002's interface mechanism against the live tenant.
`$eth-local` (folder scope) and `ethernet1/4` (device scope) are the SAME object
with the same id, `layer3` is empty on both devices, and `InterfaceRequest` would
CONFIGURE an existing interface rather than create one. Direction settled; not
built.

See `docs/DESIGN.md` for the overall Phase 1 → Phase 2 design and roadmap, and
`TODOS.md` for deferred work with the reasoning that deferred it.
