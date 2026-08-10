# intent — the Day-2 request surface (app-language, not PAN-OS)

One file per request, laid out `intent/<environment>/<team>/REQ-*.yaml`.

You declare **what you want** — source, destination, service, justification,
ticket. You never name zones, folders, address objects or rule positions: the
compiler derives all of that from your intent plus `catalog/`.

**Want a rule?** Read [`docs/requesting-rules.md`](../docs/requesting-rules.md)
— step by step, with a worked example for every kind.

**Standing up a folder?** Read
[`docs/building-a-folder.md`](../docs/building-a-folder.md) — the Day-1 chain end
to end, reconstructed from a real build.

## The four kinds

| `kind:` | What it does | Who normally writes it |
|---|---|---|
| `AccessRequest` | a security rule — allow or deny traffic | app teams |
| `ZoneRequest` | declares a zone and binds interfaces to it | platform |
| `InterfaceRequest` | addresses an interface on one firewall | platform |
| `RouteRequest` | a static route in a VRF | platform |

`AccessRequest` targets an `environment:`; the other three name a `folder:` or a
`device:` directly (ADR-0006, ADR-0007). **A `RouteRequest` is the most dangerous
of the four** — removing one black-holes traffic silently, with nothing refusing
it (ADR-0008).

## How a request reaches a firewall

```
intent/*.yaml → compile → classify (tier picks the approver) → plan → apply → enrich → push → SCM → device
```

Every merged change also writes an evidence bundle to
[`evidence/`](../evidence/), committed to Git: who asked, which ticket, what was
compiled, what the classifier decided, and who approved.

## Escape hatches

`cidr:` / `fqdn:` and raw `protocol:`+`port:` let you be explicit when the
catalog does not yet describe what you need. They are supported, not discouraged
— the catalog is meant to grow behind them.

## Intake

**Open a firewall-rule issue** and the platform generates the YAML and opens the
PR for you — no local tooling, no YAML by hand. Or write the file yourself and
open a PR; both land in the same place and go through the same gates.

`docs/requesting-rules.md` covers both.

> The Issue-Forms intake was claimed by this README from 2026-07-19 to v2.1.0
> while `.github/ISSUE_TEMPLATE/` was empty. It is real as of v2.1.0.

Git commit author is the requester identity; `metadata.requester` must match the
person asking.
