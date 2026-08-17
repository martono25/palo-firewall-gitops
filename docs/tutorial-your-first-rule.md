# Tutorial: your first firewall rule

*Tutorial — learning-oriented. You will add one real rule to a real firewall and
watch the platform check it, tier it, apply it, and record it.*

By the end you will have a working rule on the pilot firewall, an evidence bundle
proving it was authorised, and a clear idea of what happens when you open a pull
request here.

You do not need SCM credentials, cloud access, or a firewall of your own. Steps 1
to 3 run entirely on your laptop, and the platform does the rest in CI.

## What you'll need

- The repo cloned, and the tool installed: `pip install -e .`
- Nothing else. `compile` and `classify` never touch SCM — they are offline by
  design (ADR-0004), so you can iterate as fast as you can type.

Check the tool is there:

```bash
fwgitops --help
```

## Step 1: write the request

A rule is not something you write. You write a **request**, and the platform
compiles it into a rule. Create
`intent/prod/observability/REQ-2026-0899.yaml`:

```yaml
apiVersion: fw-intent/v1
kind: AccessRequest
metadata:
  id: REQ-2026-0899
  requester: you@corp
  ticket: JIRA-899
  justification: "App tier reaches the metrics collector on 9100"
  requested: 2026-08-17
spec:
  environment: prod
  action: allow
  source:
    - cidr: 10.20.1.0/24
  destination:
    - cidr: 10.20.20.10/32
  service:
    - protocol: tcp
      port: 9100
  log: true
```

Two things about that file are worth noticing now, because they explain most of
what follows.

**The `id` is the rule's name.** `REQ-2026-0899` becomes the rule's name in SCM,
its tag, and the filename of its evidence bundle. That single fact is what lets
the platform tell its own rules from a stranger's — a name is the one thing a
copy cannot inherit.

**You never named a folder, a zone, or an address object.** `environment: prod`
resolves to a folder and a zone pair; the CIDRs become address objects the
compiler names after their own contents. You said what you want; the platform
decides how.

`ticket` and `justification` are mandatory. They are not bureaucracy: they are
what makes the evidence bundle answer "who approved this, and why".

## Step 2: compile it

```bash
fwgitops compile intent
```

You will see the generated Terraform variables listed, including
`terraform/prod-edge/rules.auto.tfvars.json`. Your request is now a rule
definition.

**This is also your syntax check.** Break something on purpose — delete the
`ticket:` line and run it again. The compile fails and names the file and field.
That failure is the point: it happens on your laptop in a second, not on a
firewall in ten minutes.

Put the line back.

## Step 3: see how risky the platform thinks it is

```bash
fwgitops classify intent
```

Find your line:

```
REQ-2026-0899          LOW       allow_without_inspection
```

**LOW** means this change can apply without a human reviewer. `allow_without_inspection`
is the finding that got it noticed at all: the rule permits traffic with no
threat-inspection profile attached.

Now make it dangerous. Change the destination to `0.0.0.0/0` and re-run
`classify` — the tier rises, because the breadth check fires. Change it back.

**That tier decides who has to approve your pull request**, so you know before
you open one. LOW routes to an environment with no reviewer; anything else waits
for a human.

## Step 4: open a pull request

```bash
git checkout -b req/REQ-2026-0899
git add intent/prod/observability/REQ-2026-0899.yaml
git commit -m "feat(intent): REQ-2026-0899 — app tier to metrics collector"
git push -u origin req/REQ-2026-0899
gh pr create --fill
```

CI now runs the same `compile` and `classify` you just ran, plus a
`terraform plan` showing exactly which SCM objects your rule will create. Read
the plan. It is the last cheap moment to notice a mistake.

## Step 5: merge, and watch it land

Merge the pull request. The apply workflow starts on its own and does this, in
order:

1. **creates the address and service objects** your rule references — the API
   rejects a rule naming an object that does not exist, so this is load-bearing
2. **applies the rule** via Terraform
3. **enriches it** with the fields the provider drops, and asserts its position
4. **pushes** the candidate config to the firewall
5. **writes the evidence bundle**

Because your change is LOW, no reviewer is asked. A HIGH or CRITICAL change would
stop and wait at the `firewall-apply` gate.

## Step 6: read the evidence

After the apply, a pull request lands carrying your bundle. Merge it and look:

```bash
cat evidence/prod-edge/REQ-2026-0899.json
```

Inside: your ticket and justification, the compiled rule exactly as applied, the
risk tier with the findings that produced it, the approvers (and, honestly,
`controls_not_evidenced` when there were none), and the commit that authorised
it.

**That file is the answer to "who allowed this traffic, and on whose authority".**
It is committed to Git rather than kept in a system that expires.

## What you built

One rule on a real firewall, with a record proving it was authorised, added
without anyone touching the SCM console.

The last part matters more than it sounds. **From now on, that rule is enforced.**
If someone edits it in the console, the platform notices within a day and puts it
back. If someone adds a rule beside it by hand, that rule is deleted. Your
request is the only way that policy changes — which is what makes the evidence
bundle worth anything.

### Where to go next

- **[how-drift-enforcement-works.md](how-drift-enforcement-works.md)** — why the
  platform deletes and restores things, and what it deliberately gives up. Read
  this before you ever change anything in the console.
- **[requesting-rules.md](requesting-rules.md)** — every field you can put in a
  request, including how to turn a rule off without deleting it
- **[changing-a-rule.md](changing-a-rule.md)** — a different address, port or
  range on a rule that already exists
- **[removing-things.md](removing-things.md)** — deleting a rule, and what
  survives
- **[operator-runbook.md](operator-runbook.md)** — running it day to day, and
  what to do when drift fires

### Clean up

If you were following along on a live tenant rather than reading, remove the
intent and merge that:

```bash
git rm intent/prod/observability/REQ-2026-0899.yaml
```

Deleting the request destroys the rule (ADR-0008). That is the contract:
**nothing exists that Git does not declare.**
