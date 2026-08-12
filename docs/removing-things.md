# Removing something from a firewall

**Who this is for:** anyone who needs to take a rule, a route, a zone or an
interface address off a firewall. You do **not** need to know PAN-OS, Terraform
or how this repository is wired. You need a terminal, a ticket number, and about
fifteen minutes.

Every step below was walked end to end on a real firewall on **2026-08-12**,
removing four real objects. The outputs quoted are the ones that run produced.

---

## The idea, in three sentences

This repository is the **source of truth** for what the firewalls run. Each
thing on a firewall was created by one small YAML file in `intent/`, and the
firewall has it *because* that file exists.

**So you remove something by deleting its file.** You do not log into the
firewall, and you do not delete it in the Strata Cloud Manager web UI — do
either and the next automated run will notice the difference and put it back.

---

## Before you start

You need:

- a **ticket number** for the removal (`JIRA-1234`). Not the ticket that created
  the thing — a new one, for taking it away. Step 2 explains why.
- the repository cloned, and `fwgitops` installed:

  ```bash
  cd palo-firewall-gitops
  pip install -e .
  ```

  If that fails, you are probably not in the repository root. `ls` should show a
  `pyproject.toml`.

---

## The whole flow at a glance

1. Find the file → `fwgitops where`
2. Delete it, commit, open a pull request
3. Wait for two checks to pass
4. Merge
5. **Either it applies by itself, or it waits for a human to approve it**
6. Check the firewall actually changed

Steps 1–4 are the same for every removal. Step 5 is where they differ, and it is
the step people get wrong.

---

## Step 1 — Find the file

You know what you want gone; you probably do not know which file made it. Ask:

```bash
fwgitops where 10.100.1.110
```

You can search by IP, by CIDR, by zone name, by request id, by ticket, or by who
asked for it. Real output from 2026-08-12:

```
1 match(es) for 'JIRA-9321'

OTHER
  InterfaceRequest  REQ-2026-0805  (device:007955000902404)
      matched : metadata.ticket = JIRA-9321
      why     : metadata.ticket is exactly 'JIRA-9321'
      ticket  : JIRA-9321  (martono@corp, 2026-08-12)
      request : Address the DMZ interface on fw-prod-edge-2404 ...
      intent  : intent/prod/edge-fw-2404/REQ-2026-0805.yaml
      evidence: evidence/device-007955000902404/REQ-2026-0805.json
```

The line you need is **`intent :`** — that is the file to delete. Copy it.

Two other lines are worth reading before you delete anything:

- **`request :`** is why it was created. If that reason still holds, you may be
  about to remove something someone still needs.
- **`<- CARRIES IT`**, if it appears, means *this is the route your traffic is
  currently using*. Removing it will stop that traffic.

If `where` finds nothing, the thing you are looking at was not created from this
repository, and deleting a file here will not remove it. Stop and ask the
platform team.

→ **NEXT: Step 2, below.**

---

## Step 2 — Delete it and open a pull request

Substitute your own path and ticket:

```bash
git checkout main
git pull
git checkout -b remove/dmz-interface
git rm intent/prod/edge-fw-2404/REQ-2026-0805.yaml
```

Commit it. The commit message must contain a **`Removes:` line** naming the
request id and your ticket:

```bash
git commit -m "remove: the DMZ interface address

Decommissioning the DMZ segment; nothing is behind it any more.

Removes: REQ-2026-0805 (JIRA-9321)"
```

Push and open the pull request:

```bash
git push -u origin remove/dmz-interface
gh pr create --title "remove: the DMZ interface address" --body "Decommissioning the DMZ segment.

Removes: REQ-2026-0805 (JIRA-9321)"
```

### Two things that trip people up

**The `Removes:` line must be in the pull request BODY, not only the commit
message.** These pull requests are merged with *squash merge*, which throws away
your commit messages and uses the PR body as the message that lands. If the line
is only in your commit, the automation will not find it and the removal will be
rejected.

**The removal needs its own ticket.** Every object records the ticket that
authorised *creating* it — but you are deleting that file, so there is nowhere
left to write "and here is who approved taking it away". The `Removes:` line is
the only record, which is why it must name a real ticket.

→ **NEXT: Step 3, below.**

---

## Step 3 — Wait for the two checks

```bash
gh pr checks
```

Wait until both say `pass` — about ninety seconds:

```
compile-and-plan   pass   51s
pytest             pass   17s
```

`compile-and-plan` works out what your deletion does to the firewalls and grades
how risky it is. `pytest` checks the repository is still internally consistent.

If either fails, click the link it prints and read the error — it names the file
and what is wrong. Fix it, push again, and the checks re-run. **Do not merge a
red pull request**; the merge will be blocked anyway.

→ **NEXT: Step 4, below.**

---

## Step 4 — Merge

```bash
gh pr merge --squash --delete-branch
```

Merging starts the change. Nothing has touched a firewall yet.

→ **NEXT: Step 5, below — and read it before you walk away.**

---

## Step 5 — It either applies itself, or it waits for you

When you merged, the change was graded. What happens now depends entirely on
that grade, and **the two outcomes look completely different**.

### If it was graded LOW — it is already running

Nobody is asked. The change applies within a couple of minutes. This is normal
and intended: withdrawing an `allow` rule takes access away, and taking access
away cannot open anything up.

Skip to step 6.

### If it was graded HIGH — nothing will happen until a human approves it

The run **stops and waits**, and it will wait forever. There is no timeout and
no reminder. A removal left un-approved simply never happens, and the firewall
quietly keeps running the thing you thought you deleted.

To approve it:

```bash
gh run list --workflow apply.yml --limit 1
```

Open that run in a browser:

```
https://github.com/<owner>/<repo>/actions/runs/<the-id>
```

At the top of the page is a box saying **`firewall-apply` is waiting for
approval**, with a **Review deployments** button. Click it, tick the
`firewall-apply` checkbox, and click **Approve and deploy**.

> **Approving the pull request is not the same thing** and does not release this.
> The deployment gate is separate, it lives on the *run* page rather than the PR
> page, and this is the single most common place to get stuck. On 2026-08-12 it
> cost us twenty minutes: the approval had been given on the wrong page, the run
> sat waiting, and from the pull request everything looked finished.
>
> To check whether an approval actually registered:
>
> ```bash
> gh api repos/<owner>/<repo>/actions/runs/<id>/pending_deployments \
>   -q '.[].environment.name'
> ```
>
> If that prints `firewall-apply`, it is **still waiting** — whatever you clicked
> did not land.

Which grade you get is not a judgement call, and it is worth knowing in advance:

| You remove | Grade | Waits for approval? |
|---|---|---|
| an `allow` rule | LOW | no — applies by itself |
| a `deny` rule | HIGH | yes |
| a route | HIGH | yes |
| a zone | HIGH | yes |
| an interface address | HIGH | yes |

The one that surprises people is the first row. Removing a rule that *permitted*
something is low risk, because the firewall ends up allowing less. Removing a
rule that *blocked* something is high risk, because traffic it was stopping may
now be allowed by some other rule. The grade follows which direction access
moves, not how big the change feels.

→ **NEXT: Step 6, below.**

---

## Step 6 — Check the firewall actually changed

**A green run does not mean the firewall has your change yet.** The run reports
success when Strata Cloud Manager accepts the change; the firewall applies it
some time afterwards.

Measured on 2026-08-12, on the same firewall, an hour apart:

| Change | How long after the run said success |
|---|---|
| a route removed | between 9 seconds and 48 seconds |
| a zone and a route restored | between 1 m 20 s and 3 m 25 s |

So: **seconds to minutes.** Do not conclude it failed because it is not there
after thirty seconds. Look again in five minutes.

The safe check, which works for any change:

```bash
fwgitops device-sync
```

That reports, per firewall, whether it is running what this repository says it
should be. To look at the firewall directly:

```bash
printf 'set cli pager off\nshow interface all\n' \
  | ssh -T -i fwgitops-pilot.pem admin@<firewall-mgmt-ip>
```

Real before and after from 2026-08-12, removing `REQ-2026-0805`:

```
before:  ethernet1/3   18   1   dmz    lr:default   0   10.100.1.110/24
after:   ethernet1/3   18   1                       0   N/A
```

The interface still exists; its address is gone. That is what "removed" means
for an interface — see the table below for the other kinds.

→ **Done.** If something looks wrong, see *When it does not work* below.

---

## What "removed" actually means, per kind

Each of these was measured on 2026-08-12 by doing it.

| You remove | What happens on the firewall |
|---|---|
| **a rule** (`AccessRequest`) | The rule is gone. Traffic it allowed is now decided by whatever rule sits below it — often a default deny. Any address objects only it used are cleaned up too. |
| **a zone** (`ZoneRequest`) | The zone is deleted, and **any interface that was in it is left with an address but no zone**. PAN-OS drops all traffic on an interface with no zone, so that segment goes dark. The run still reports success and warns you about nothing. |
| **a route** (`RouteRequest`) | The route disappears and traffic that used it stops being forwarded. Nothing anywhere refuses this — a router with one fewer route is still perfectly valid — so the grade and your ticket are the only signals that anything significant happened. |
| **an interface address** (`InterfaceRequest`) | The interface keeps existing but loses its IP. Anything reaching the firewall on that address stops. |

### One case where the removal will fail, on purpose

If you delete a **zone that a rule still refers to**, Strata Cloud Manager
refuses it and the run fails with a `409` error. That failure is correct and
useful — it is stopping you from breaking the rule. Delete the rule first, then
the zone.

Note the asymmetry, because it caught us out: an **interface** sitting in the
zone does *not* block the deletion. Only a **rule** does. On 2026-08-12 the zone
deleted in two seconds with an interface still in it, reported success, and left
that interface dark.

---

## When it does not work

### The run failed after I merged

**Do not "just run it again" from the Actions tab.** Re-running compares against
the wrong starting point — one where your file is already gone — so your change
stops being recognised as a removal. The object gets taken off the firewall with
**no audit record that anyone removed it**. A deletion nobody can account for is
the exact thing this system exists to prevent.

Instead:

1. Put the file back in a new pull request. This changes nothing on the firewall
   — the object is still there, because the removal failed.
2. Fix whatever the failure was.
3. Remove it again as a fresh change.

### I merged it and nothing is happening

It is almost certainly waiting for approval. See step 5 — check
`pending_deployments`, and make sure you approved on the **run** page.

### `fwgitops where` cannot find my thing

It was not created from this repository. Deleting a file here will not remove
it. Ask the platform team before touching it anywhere else.

---

## Where to go next

- **Adding a rule instead:** [`requesting-rules.md`](requesting-rules.md), from
  the top.
- **The day-to-day operator reference** — drift, failed pushes, replacing a
  firewall: [`operator-runbook.md`](operator-runbook.md).
- **Why removal behaves this way per kind**, in design terms:
  [`adr/0008-deletion-contract.md`](adr/0008-deletion-contract.md).
