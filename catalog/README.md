# catalog — platform-maintained resolution data

What lets a broad requester say `app: payments-api` instead of a subnet. The compiler resolves
intent against this catalog (stages 2 + 4 of the pipeline). Platform/security team owns it;
changes to the catalog are themselves PRs.

Contents:
- `apps.yaml`      — app name → subnets/address objects + which environment/folder + zone
- `services.yaml`  — friendly service name → protocol/port(s)
- `environments.yaml` — environment → folder + default zone-pair (resolve.py)
- `zones.yaml`     — per-environment IP-to-zone map (drives zone inference) *(planned)*

**Reference allowlists (ADR-0003)** — validate a rule's named references so a typo fails at
PR time, not at the SCM device commit. Each is a `NameCatalog`; **absent = that field is
accepted free-form** (no false confidence), present-but-malformed = hard error.
- `applications.yaml`    — vetted App-ID allowlist for `spec.application` (`any` always valid).
                           Authoritative universe: PANW Applipedia.
- `profiles.yaml`        — security profile GROUP names for `spec.profile`.
                           Ships as `profiles.example.yaml` — copy + fill with your REAL SCM
                           groups (listing non-existent ones re-introduces the commit-time failure).
- `log-forwarding.yaml`  — log-forwarding profile names for `spec.log_forwarding`.
                           Ships as `log-forwarding.example.yaml` — same rule.

Phase 1 can run with a thin catalog + explicit CIDR/FQDN fallback; the catalog fills in over
Phase 2-3 as more apps onboard.
