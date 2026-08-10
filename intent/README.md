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
intent/*.yaml → compile → risk gate → terraform plan → apply → enrich → push → SCM → device
```

Every merged change also writes an evidence bundle to
[`evidence/`](../evidence/), committed to Git: who asked, which ticket, what was
compiled, what the classifier decided, and who approved.

## Escape hatches

`cidr:` / `fqdn:` and raw `protocol:`+`port:` let you be explicit when the
catalog does not yet describe what you need. They are supported, not discouraged
— the catalog is meant to grow behind them.

## Intake

**Open a PR against this directory.** `docs/requesting-rules.md` walks through it
entirely in the GitHub web UI; no local tooling is needed.

> An Issue-Forms intake (`.github/ISSUE_TEMPLATE/` → an Action that generates the
> YAML and opens the PR) is designed but **not built**. This README claimed it
> existed from 2026-07-19 until v2.0.0, which is the kind of false claim this
> project exists to remove. Tracked in [`TODOS.md`](../TODOS.md).

Git commit author is the requester identity; `metadata.requester` must match the
person asking.
