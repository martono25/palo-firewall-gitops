# Changing a rule that already exists

**Who this is for:** anyone who needs an existing firewall rule to cover
something different — a new address, a different port, a wider or narrower
range. You do **not** need to know PAN-OS or Terraform.

Adding a rule is [`requesting-rules.md`](requesting-rules.md). Removing one is
[`removing-things.md`](removing-things.md). This is the one in between, and it
behaves differently from both.

Every step here was walked on a real firewall on **2026-08-13**, twice — once
narrowing a rule and once widening it. The outputs quoted are from those runs.

---

## The one thing to understand first

**Changing a rule is not "edit the firewall".** You edit the file that created
the rule, and the platform works out the difference.

You also do not edit the rule *in place* in any meaningful sense. If you change
an address, the platform creates a new address object, points the rule at it,
and later cleans up the old one. That matters for exactly one reason, explained
at the bottom under *Why your change creates and deletes things you did not ask
for* — you can skip it unless something looks odd.

---

## Before you start

You need a **new ticket number**. Not the one that created the rule — a new one,
for this change. The platform rejects a changed rule that still carries its
original ticket, and the error is explicit about it:

```
REQ-2026-0809 (AccessRequest): `spec` changed but `metadata.ticket` is still
'JIRA-20809'. A change needs its own change ticket — otherwise the evidence
bundle for this change names the request that authorised the PREVIOUS one.
```

That is not bureaucracy. The audit record for this change would otherwise point
at a request that approved something else.

---

## Step 1 — Find the file

```bash
fwgitops where 10.20.1.55
```

Search by IP, CIDR, zone name, request id, ticket, or requester. Copy the
**`intent :`** line — that is the file you edit:

```
  AccessRequest  REQ-2026-0809  (prod-edge)
      ticket  : JIRA-21001  (martono@corp, 2026-08-12)
      intent  : intent/prod/observability/REQ-2026-0809.yaml
      evidence: evidence/prod-edge/REQ-2026-0809.json
```

→ **NEXT: Step 2, below.**

---

## Step 2 — Edit the file

```bash
git checkout main && git pull
git checkout -b update/collector-ping
```

Open the file and change **three** things:

1. **What you actually want different** — the address, the port, the service.
2. **`metadata.ticket`** — your new ticket.
3. **`metadata.justification`** — why *this* change, not why the rule exists.

```yaml
metadata:
  id: REQ-2026-0809          # NEVER change this — it is the rule's identity
  ticket: JIRA-21001         # <- your new ticket
  justification: "Narrow the collector ping to the one host that answers it"
  requested: 2026-08-13      # <- today
spec:
  destination:
    - cidr: 10.20.1.55/32    # <- the change
```

> **Do not change `metadata.id`.** That is what makes this a change to an
> existing rule rather than a brand-new one. Change it and you get a second rule
> alongside the first, and the original stays exactly where it was.

Commit and open the pull request:

```bash
git commit -am "update: narrow the collector ping to one host

Narrows REQ-2026-0809 from the whole tier to the host that answers."
git push -u origin update/collector-ping
gh pr create --fill
```

Unlike a removal, **a change needs no special trailer in the PR body.** The new
ticket in the file is the authorisation.

→ **NEXT: Step 3, below.**

---

## Step 3 — Read the risk grade before you merge

Wait for the checks (`gh pr checks`), then look at what `compile-and-plan` says:

```
REQ-2026-0809          LOW       allow_without_inspection
```

**The grade follows the direction access moves, not how big the edit looks.**

| You are… | Grade | What happens on merge |
|---|---|---|
| **narrowing** — fewer addresses, fewer ports | LOW | applies by itself |
| **widening** — more addresses, more ports | HIGH | waits for a human |

Both were measured on 2026-08-13 on the same rule. Narrowing
`10.20.1.0/24 → 10.20.1.55/32` graded LOW and applied unattended. Widening it to
`10.20.0.0/16` graded HIGH `broad_destination` and stopped for a reviewer.

The logic is the same one that governs removals: taking access away can break
something, but it cannot open anything. Granting access can.

→ **NEXT: Step 4, below.**

---

## Step 4 — Merge, and approve if it waits

```bash
gh pr merge --squash --delete-branch
```

**If it graded LOW**, it is already running. Skip to step 5.

**If it graded HIGH**, the apply stops and waits — and this catches people out:

> **The approval you gave on the pull request is not this one.** This second
> approval lives on the **run** page: repo → **Actions** → **apply** → your run
> → **Review deployments** → tick `firewall-apply` → **Approve and deploy**.
>
> Nothing chases it. An un-approved run waits forever, the firewall never gets
> your change, and from the merged pull request everything looks finished.
>
> To check whether an approval actually registered:
>
> ```bash
> gh api repos/<owner>/<repo>/actions/runs/<run-id>/pending_deployments \
>   -q '.[].environment.name'
> ```
>
> Printing `firewall-apply` means it is **still waiting**.

→ **NEXT: Step 5, below.**

---

## Step 5 — Confirm the firewall took it

**A green run does not mean the firewall has your change yet** — it means
Strata Cloud Manager accepted it. The device follows seconds to minutes later.
Measured on the two updates on 2026-08-13: 1 m 15 s – 2 m 24 s, and 71 s –
2 m 12 s.

```bash
fwgitops device-sync
```

Or look at the rule on the firewall itself:

```bash
printf 'set cli pager off\nshow running security-policy\n' \
  | ssh -T -i fwgitops-pilot.pem admin@<firewall-mgmt-ip> \
  | grep -A6 REQ-2026-0809
```

Real before and after from the narrowing:

```
before:  destination 10.20.1.0/24;
after:   destination 10.20.1.55;
```

→ **Done.**

---

## Why your change creates and deletes things you did not ask for

You will see the run mention address objects being created and removed even
though you only edited one line. That is normal, and it is worth thirty seconds
of your time because it explains an error you might otherwise misread.

Addresses and services are **named after their value** — `10.20.1.55/32` is
always the object `addr-a102bfc799`, everywhere, forever. So identical values
collapse into one shared object: three rules using `10.20.1.0/24` share a single
one.

That sharing is why an object cannot simply be edited. Changing its value would
silently change every other rule pointing at it. So changing a rule's
destination is really:

1. create the object for the new value (if no rule already uses it)
2. point the rule at it
3. the old object is now unused — collected later, once nothing references it

Step 3 happens **after** the change reaches the firewall, deliberately. Before
ADR-0010 it happened during the same apply, and on 2026-08-13 that failed:

```
Error deleting addresses / 409 Conflict
errorType: Reference Not Zero
Node cannot be deleted because of references from params:[addr-a102bfc799]
```

The cleanup ran before the rule update that released the object. The apply
aborted and the firewall was left untouched — correctly, but for a change that
was perfectly valid. Splitting the two apart is what fixed it.

**If you see `NON_ZERO_REFS` on your change**, that is what it means: something
still refers to an object being deleted. It is a platform issue, not a mistake
in your request — bring it to the platform team rather than editing your file.

---

## When it does not work

### The checks say my ticket is stale

You changed the rule but not `metadata.ticket`. See *Before you start*.

### I changed the file but a second rule appeared

You changed `metadata.id`. That is the rule's identity — a new id is a new rule.
Put the original id back; the extra rule needs removing via
[`removing-things.md`](removing-things.md).

### The run is green but the firewall still shows the old value

Give it a few minutes and look again (step 5). If it persists,
`fwgitops device-sync` will say whether the firewall and the repository disagree.

### The apply failed after I merged

**Do not re-run it from the Actions tab.** Take it to the platform team with the
run link. Re-running compares against the wrong starting point and can make
things worse — the same trap described in
[`removing-things.md`](removing-things.md).

---

## Where to go next

- **Adding a new rule:** [`requesting-rules.md`](requesting-rules.md)
- **Removing one:** [`removing-things.md`](removing-things.md)
- **Why objects behave this way:**
  [`adr/0010-address-and-service-object-lifecycle.md`](adr/0010-address-and-service-object-lifecycle.md)
