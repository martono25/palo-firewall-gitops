# intent — Day-2 request surface (app-language, not PAN-OS)

One file per change, laid out `intent/<environment>/<app>/REQ-*.yaml`. Requesters (broad
audience) declare WHAT they want in app/business terms — source, destination, service,
justification, ticket. They never specify zones, folders, objects, or rule positions; the
compiler derives all of that from the intent + `catalog/`.

Escape hatches (`cidr`/`fqdn`, raw `protocol/port`) let power users be explicit and keep the
schema usable before the catalog is complete. See `docs/DESIGN.md` → "Day-2 Intent Schema &
Compiler" for the full v1 schema and the 11-stage pipeline. Example:
`prod/payments-api/REQ-2026-0417.example.yaml`.

Intake: broad requesters use GitHub Issue Forms (`.github/ISSUE_TEMPLATE/`) → an Action
generates the intent YAML and opens the PR. Git commit author = requester identity.
