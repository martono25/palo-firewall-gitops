# catalog — platform-maintained resolution data

What lets a broad requester say `app: payments-api` instead of a subnet. The compiler resolves
intent against this catalog (stages 2 + 4 of the pipeline). Platform/security team owns it;
changes to the catalog are themselves PRs.

Planned contents:
- `apps.yaml`      — app name → subnets/address objects + which environment/folder + zone
- `services.yaml`  — friendly service name → protocol/port(s)
- `zones.yaml`     — per-environment IP-to-zone map (drives zone inference)

Phase 1 can run with a thin catalog + explicit CIDR/FQDN fallback; the catalog fills in over
Phase 2-3 as more apps onboard. Seeding the initial catalog is a named open task.
