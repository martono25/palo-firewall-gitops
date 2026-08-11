# Requesting a firewall rule

This guide walks you through requesting a firewall rule change **from start to
finish**, using only your web browser. You describe *what you need* in a small
file, open a pull request, and — once approved and merged — the platform applies
it to the firewall automatically.

You do **not** need to install anything, and you do **not** need to know
Terraform, the SCM API, or PAN-OS internals. Just follow the steps.

> **Two ways to do this:** [**Part 1 — Website**](#part-1--the-website-walkthrough)
> (recommended, nothing to install) and
> [**Part 2 — Command line**](#part-2--command-line-alternative) (for developers).
> Everything else — the field reference, examples, and troubleshooting — is at the
> bottom.

---

## Updating or removing an existing rule

**To change a rule**, edit its file and **give it a new `ticket`**. The request id
stays the same — it is the rule's name on the firewall — but the ticket must
describe THIS change, not the one that created the rule.

That is enforced: a PR that changes `spec` while leaving the old ticket in place
is rejected. Without it, the audit record for your change would name the request
that authorised the previous version — a different person, a different date, and
a justification for a different rule.

**To remove a rule**, delete its file **and put a `Removes:` line in your PR
description**:

```
Removes: REQ-2026-0142 (JIRA-31555)
```

That is required, and a PR without it is rejected. The reason is the same as for
an update: the ticket inside the file authorised CREATING the rule, and deleting
the file leaves nowhere to record who authorised removing it — so the trailer
carries that. Put it in the PR description, because a squash merge is what lands
on `main` and that is the text the pipeline reads.

There is no "delete request" file to write. The PR diff shows the whole rule
being removed, and the pipeline classifies the removal on its own terms —
removing a `deny` is treated more seriously than removing an `allow`, because
traffic the deny blocked may now match a permissive rule below it.

**To see a rule's history**, follow its file: `git log --follow
intent/<env>/<app>/REQ-....yaml`.

---

## Before you start

- A **GitHub account** that has been added to the `martono25/palo-firewall-gitops`
  repository. If you can't open <https://github.com/martono25/palo-firewall-gitops>,
  ask the platform team to grant you access. **This is the only prerequisite for
  the website path.**
- Decide your request's **id** (the rule name), e.g. `REQ-2026-0142`. It must be
  unique (no existing rule with that id) and use only letters, digits, `.`, `_`,
  `-`. Convention is `REQ-<year>-<number>`, but any unique id works.

---

## The quickest way: open an issue

**[New issue → Firewall rule request](../../issues/new?template=rule-request.yml)**

Fill in the form — ticket, why, source, destination, service — and submit. The
platform generates the intent YAML and opens a pull request with it, then
comments on your issue with the link.

You never write YAML, and you never need to know what a zone or a folder is.

If something in the form cannot be read, **the issue gets a comment naming the
field and what to write instead** — edit the issue and it tries again. No need to
open a new one.

The generated PR is a normal request from there on: same validation, same risk
tiering, same approval. The form is a way to *write* a request, not a way to skip
reviewing one.

> Everything below is the manual route — write the file yourself and open the PR.
> Both land in the same place. Read on if you want to understand the file the
> form produces, or if you are editing an existing rule.

## Part 1 — the website walkthrough

### Step 1. Open the repository

Go to <https://github.com/martono25/palo-firewall-gitops> and make sure you are on
the **`main`** branch (the branch dropdown, top-left of the file list, should say
`main`).

### Step 2. Start a new request file

1. Click **Add file** (top-right of the file list) → **Create new file**.
2. In the filename box at the top, type the **full path**, using your team name
   and your request id. Typing `/` creates folders as you go:
   ```
   intent/prod/payments/REQ-2026-0142.yaml
   ```
   - `prod` = the environment (which firewall). Valid environments are listed in
     `catalog/environments.yaml`.
   - `payments` = your app or team (any short name — just for organisation).
   - `REQ-2026-0142.yaml` = your request id + `.yaml`.

### Step 3. Paste your request and fill it in

Paste this template into the editor, then change the values to what you need.
Everything under `# ` is a comment you can delete or leave.

```yaml
apiVersion: fw-intent/v1
kind: AccessRequest
metadata:
  id: REQ-2026-0142                 # <- must match your filename; becomes the rule name
  requester: you@corp.com           # <- your email
  ticket: JIRA-4821                 # <- your change ticket
  justification: "Web tier needs to reach the payments API"   # <- why, one line
  requested: "2026-08-01"           # <- today's date, YYYY-MM-DD
spec:
  environment: prod                 # <- which firewall (see catalog/environments.yaml)
  action: allow                     # <- allow | deny | drop | reset-both | reset-client | reset-server
  source: [{cidr: "10.20.1.0/24"}]        # <- where the traffic comes FROM
  destination: [{cidr: "10.20.9.10/32"}]  # <- where it goes TO
  service: [{protocol: tcp, port: "443"}] # <- protocol + port
```

That's a complete, valid request. Optional fields (App-ID, inspection profile,
logging, user/URL matching, ordering) are in the [field reference](#field-reference)
below — add them if you need them.

### Step 4. Propose the change (create the pull request)

1. Scroll to the bottom of the page to the **Commit changes** box.
2. Choose **"Create a new branch for this commit and start a pull request."**
   (GitHub picks a branch name for you — you can accept it.)
3. Click **Propose changes**.
4. On the next screen, give it a title (e.g. *"Request REQ-2026-0142: web →
   payments"*) and click **Create pull request**.

You are now on your pull request page. **You do not need to know or type a PR
number anywhere** — every action from here is a button on this page.

### Step 5. Wait for the automatic checks (~1–2 minutes)

Near the bottom of the PR page you'll see a checks section:

- **Green check ✅** — your request is valid. Scroll up to the **bot comment**: it
  shows the **risk level** and a **preview** of the exact rule (App-ID, profile,
  ordering) plus the Terraform plan. Continue to Step 6.
- **Red ✗** — something needs fixing. Click **Details** to read the exact message
  (e.g. *"spec.action: must be one of …"*). Then fix it: click the **Files
  changed** tab → the pencil ✏️ on your file → edit → **Commit changes** to *the
  same branch*. The checks re-run automatically.

You never run any command to fix things — you edit the file on the website.

### Step 6. Get it reviewed and merge

1. Ask a reviewer (per your team's process) to approve — they read the risk +
   preview and approve on the PR page.
2. Once approved and the checks are green, click the green **Merge pull request**
   button → **Confirm merge**.

### Step 7. It goes live automatically — then confirm it

Merging kicks off the **apply** automatically — you don't run anything. To check
that your rule actually deployed, go to
**[Confirm your rule deployed](#confirm-your-rule-deployed)** below (it covers both
the website and the command line).

> **Risk note:** LOW-risk changes apply automatically on merge. HIGH/CRITICAL ones
> (broad `0.0.0.0/0`, exposing risky ports from the internet, negated matches, an
> uninspected allow, or a brand-new zone path) are held for an explicit approval
> step and will **not** auto-apply — the PR check tells you which tier you're in.

---

## Part 2 — command line alternative

For developers who prefer the terminal. One-time setup: install **Git**
(<https://git-scm.com/downloads>), optionally the **GitHub CLI**
(<https://cli.github.com/>, then `gh auth login`), and clone the repo:

```bash
git clone https://github.com/martono25/palo-firewall-gitops.git
cd palo-firewall-gitops           # <- run everything from INSIDE this folder
```

Then, per request (run all of this from inside the repo folder):

```bash
# 1. start from the latest main, on a new branch
git checkout main && git pull
git checkout -b req/REQ-2026-0142        # branch name = anything unique

# 2. create your request file (any editor)
mkdir -p intent/prod/payments
nano intent/prod/payments/REQ-2026-0142.yaml    # paste the template from Step 3

# 3. commit, push, open the PR
git add intent/prod/payments/REQ-2026-0142.yaml
git commit -m "request: REQ-2026-0142 web -> payments"
git push -u origin req/REQ-2026-0142
gh pr create --fill                       # prints your PR's URL

# 4. after checks pass and it's approved — merge (no PR number needed:
#    this merges the PR for the branch you're currently on)
gh pr merge --squash --delete-branch
```

You do **not** run `fwgitops` or compile anything — CI does that. You only edit
files under `intent/` and open/merge the PR.

If a check fails: edit the file, then
`git add … && git commit --amend --no-edit && git push -f`, and the checks re-run.

After merge, confirm it deployed — see
**[Confirm your rule deployed](#confirm-your-rule-deployed)** below. From the
command line the quickest check is:

```bash
set -a; source ~/.fwgitops/scm.env; set +a          # once per shell (SCM creds)
fwgitops rules prod-edge --has REQ-2026-0142         # LIVE (exit 0) / NOT FOUND (exit 3)
```

---

## Confirm your rule deployed

Two ways, both **without logging into SCM**. Use whichever fits your path.

### From GitHub (website)

1. Repo → **Actions** tab → **apply** workflow → the run named after your merge
   (it starts within seconds of merging — **you don't run it, the merge does**).
2. Wait ~2 min. **Green ✅** = deployed; **red ✗** = failed and *nothing changed*
   (fail-closed) — open the run to read why.
3. At the top of the run page, the **"Firewall rules deployed"** summary lists
   every live rule, including yours:
   ```
   Firewall rules deployed
   Result: success
   Folder `prod-edge`:
     - REQ-2026-0142      <- your rule
     - ...
   ```
   Seeing your `id` = it's live. *(Deeper proof: the run's **Artifacts** include an
   evidence bundle — a JSON audit record of exactly what was applied.)*

### From the command line (any time)

Reads SCM's current state, so it works **whenever** — not only right after a
deploy. Needs the `fwgitops` CLI + `SCM_*` credentials (operator setup):

```bash
set -a; source ~/.fwgitops/scm.env; set +a           # once per shell (SCM creds)

fwgitops rules prod-edge --has REQ-2026-0142
#   REQ-2026-0142: LIVE in folder 'prod-edge'      (exit 0)
#   REQ-2026-0142: NOT FOUND in folder 'prod-edge' (exit 3)

fwgitops rules prod-edge          # or list everything live in the folder
```

Once live, if the firewall has a device attached, the rule reaches the device
shortly after (a brand-new device can take 20–30 min on its very first sync).

---

## Field reference

### `metadata` (all fields required)

| Field | Meaning |
|---|---|
| `id` | Your request id — becomes the rule name. Letters/digits/`.`/`_`/`-` only, unique. |
| `requester` | Your email. |
| `ticket` | Change ticket id (audit linkage). Same character rules as `id`. |
| `justification` | One line: why you need this. |
| `requested` | Date requested, `YYYY-MM-DD`. |

> There is **no `expires` field**. It was removed in v1.23.0 and is now REJECTED,
> not ignored: nothing ever read it, so a rule with an expiry date sat there
> looking governed and expired on nothing. To retire a rule, remove it (below).

### `spec`

| Field | Required | Default | Meaning |
|---|:--:|---|---|
| `environment` | ✅ | — | Which firewall/folder. Valid values in `catalog/environments.yaml`. |
| `action` | ✅ | — | `allow` · `deny` · `drop` · `reset-client` · `reset-server` · `reset-both`. |
| `source` | ✅ | — | List of endpoints (see **Endpoints**). |
| `destination` | ✅ | — | List of endpoints. |
| `service` | ✅ | — | List of services (see **Services**). |
| `application` | | `["any"]` | App-ID names, e.g. `["ssl","web-browsing"]`. Port-only if omitted. |
| `profile` | | *(none)* | Security profile **group** → threat inspection. Omitted = **no inspection** (flagged, not blocked). |
| `log_forwarding` | | *(none)* | Log-forwarding profile (send logs off-box). |
| `source_user` | | `["any"]` | User-ID users/groups. Reserved: `any`, `pre-login`, `known-user`, `unknown`. |
| `category` | | `["any"]` | URL categories, e.g. `["financial-services"]`. |
| `negate_source` | | `false` | Match everything **except** the source. |
| `negate_destination` | | `false` | Match everything **except** the destination. |
| `log` | | `true` | Log at session **end**. |
| `log_start` | | `false` | Also log at session **start**. |
| `description` | | *(none)* | Free-text rule note. |
| `position` | | *(unspecified)* | Ordering: `top` · `bottom` · `before:<rule-id>` · `after:<rule-id>`. **Omit it unless you mean it** — see below. |

#### A word on `position`

**Leave it out unless the rule's placement actually matters.** Omitted is not the
same as `bottom`: an omitted position means *"I have no opinion"*, and the
platform then leaves the rule where the firewall puts it. Naming a position is an
instruction to MOVE the rule, and rule order is policy — a permissive rule above a
deny is a different firewall.

Until v2.0.0 this defaulted to `bottom`, which made "I didn't ask" indistinguishable
from "put it at the bottom" and would have silently re-stacked live rulebases.

`before:`/`after:` are applied after the rule exists, so the rule they name must
be in the same folder.

### Endpoints (`source` / `destination`)

Each entry is exactly one of:

```yaml
- {cidr: "10.20.1.0/24"}      # a network (use the network address, not a host in it)
- {cidr: "10.20.9.10/32"}     # a single host
- {fqdn: "payments.internal"} # a hostname
- {app: "payments-api"}       # a named app from catalog/apps.yaml
```

### Services

```yaml
- {protocol: tcp, port: "443"}         # a port
- {protocol: udp, port: "53"}
- {protocol: tcp, port: "8000-8100"}   # a range
- {name: "https"}                       # a named service from catalog/services.yaml
- {protocol: icmp}                      # ping — NO port (ICMP has none)
```

**ICMP / ping.** Write `{protocol: icmp}` with **no `port`** — ICMP has no ports,
and a `port` alongside it is rejected rather than ignored, because a number that
looks like a restriction and enforces nothing is worse than no number at all.

Two things follow from how PAN-OS matches ICMP:

* It is matched by **application**, not by a port-based service, so the compiler
  emits the `ping` App-ID for you. Do **not** also set `application:` on an ICMP
  request.
* **Do not mix `icmp` with `tcp`/`udp` in one request** — it is rejected.
  `service` is a rule-level list, so an ICMP entry changes what the port entries
  beside it match. File them as two requests, each meaning what it says.

```yaml
  service:
    - protocol: icmp
```

---

## Examples

Every example below is a **complete file** — copy the whole thing into a new
`intent/prod/<team>/REQ-<id>.yaml` and change the values.

**Ping, for monitoring (ICMP):**
```yaml
apiVersion: fw-intent/v1
kind: AccessRequest
metadata:
  id: REQ-2026-0210
  requester: you@corp.com
  ticket: JIRA-12345
  justification: "Collector must ping the web tier to tell a dead host from a dead service"
  requested: 2026-08-10
spec:
  environment: prod
  action: allow
  source:
    - cidr: 10.20.20.10/32
  destination:
    - cidr: 10.20.1.0/24
  service:
    - protocol: icmp        # no port
  log: true
```

**Inspected HTTPS allow (App-ID + profile + logging):**
```yaml
apiVersion: fw-intent/v1
kind: AccessRequest
metadata:
  id: REQ-2026-0201
  requester: you@corp.com
  ticket: JIRA-5001
  justification: "Web tier to payments API, HTTPS only, inspected"
  requested: "2026-08-01"
spec:
  environment: prod
  action: allow
  source: [{cidr: "10.20.1.0/24"}]
  destination: [{fqdn: "payments.internal"}]
  service: [{protocol: tcp, port: "443"}]
  application: ["ssl", "web-browsing"]
  profile: best-practice
  log_forwarding: log-best
```

**Block (drop) telnet to a host:**
```yaml
apiVersion: fw-intent/v1
kind: AccessRequest
metadata:
  id: REQ-2026-0202
  requester: you@corp.com
  ticket: JIRA-5002
  justification: "Block legacy telnet to the jump host"
  requested: "2026-08-01"
spec:
  environment: prod
  action: drop
  source: [{cidr: "10.20.2.0/24"}]
  destination: [{cidr: "10.20.9.11/32"}]
  service: [{protocol: tcp, port: "23"}]
```

**Allow a specific user group only (User-ID):**
```yaml
apiVersion: fw-intent/v1
kind: AccessRequest
metadata:
  id: REQ-2026-0203
  requester: you@corp.com
  ticket: JIRA-5003
  justification: "Only the payments-admins group may reach the admin console"
  requested: "2026-08-01"
spec:
  environment: prod
  action: allow
  source: [{cidr: "10.20.4.0/24"}]
  destination: [{cidr: "10.20.9.13/32"}]
  service: [{protocol: tcp, port: "443"}]
  source_user: ["corp\\payments-admins"]
```

---

## Common mistakes

| Message / symptom | Fix |
|---|---|
| PR check red, `spec.action: must be one of [...]` | Use a valid action (see the table). |
| `invalid CIDR ... (host bits set …)` | Use the network address (`10.20.1.0/24`, not `10.20.1.5/24`). |
| `unknown environment 'staging'; known: [...]` | Use an environment from `catalog/environments.yaml`. |
| `unknown App-ID / security profile group / …` | Fix the name, or ask the platform team to add it to the relevant `catalog/*.yaml`. |
| Nothing changed on the firewall after opening the PR | **A PR only proposes the change — you must *merge* it.** The rule applies on merge. |
| Change is **HIGH/CRITICAL** and waits for a reviewer | Expected for broad/any-any/exposed/negated rules — get the approval, or tighten the rule. |

---

## The other three kinds

Most requests are `AccessRequest` — a rule. Three other kinds exist, normally
written by the platform team, and they are documented here because **you will see
them in this directory and their failure modes are worse than a rule's**
(ADR-0008 measured each one on hardware).

> Standing up a whole folder rather than reading one of these? See
> [`building-a-folder.md`](building-a-folder.md) — the Day-1 chain end to end,
> reconstructed from how `prod-edge` was actually built.

### `ZoneRequest` — declare a zone, bind interfaces to it

```yaml
apiVersion: fw-intent/v1
kind: ZoneRequest
metadata:
  id: REQ-2026-0301
  requester: you@corp.com
  ticket: JIRA-12345
  justification: "DMZ zone on the edge firewall, bound to the DMZ interface"
  requested: 2026-08-10
spec:
  folder: prod-edge          # or device: <serial>
  zone: dmz
  type: layer3
  interfaces: ["$eth-dmz"]
```

**On removal:** SCM REFUSES to delete a zone while any rule references it (409).
Unreferenced, it deletes — and any interface bound to it is left **addressed but
unzoned**, which PAN-OS treats as unusable: traffic on it is dropped.

### `InterfaceRequest` — put an address on one firewall's interface

```yaml
apiVersion: fw-intent/v1
kind: InterfaceRequest
metadata:
  id: REQ-2026-0302
  requester: you@corp.com
  ticket: JIRA-12346
  justification: "Address the DMZ interface on the edge firewall"
  requested: 2026-08-10
spec:
  device: "007955000901881"   # a firewall SERIAL — addressing is per-device
  interface: dmz              # a ROLE from catalog/interfaces.yaml, not a port
  ip:
    - 10.100.3.1/24
```

It **configures an interface that already exists** (ADR-0005) — it cannot create
one. `device:` not `folder:`, because two firewalls cannot share an IP.

`interface:` takes a **role** (`local`, `internet`, `dmz` — see
`catalog/interfaces.yaml`), not a port name and not the `$eth-` variable. The
catalog maps the role to the physical port, which is what stops a requester
conjuring an interface the firewall does not have.

**On removal:** the device-scope override reverts to the inherited object, which
carries **no addressing** — the firewall loses the IP on that interface.

### `RouteRequest` — a static route. The most dangerous kind.

```yaml
apiVersion: fw-intent/v1
kind: RouteRequest
metadata:
  id: REQ-2026-0303
  requester: you@corp.com
  ticket: JIRA-12347
  justification: "Default route out the untrust interface"
  requested: 2026-08-10
spec:
  folder: prod-edge
  destination: 0.0.0.0/0
  nexthop: 10.100.2.1
```

**On removal: NOTHING REFUSES IT, at any layer.** Measured 2026-08-06 — the
default route disappeared from the device about 40 seconds after the push
reported success. Connected routes survived, intra-subnet traffic kept working,
and **everything off-subnet was black-holed**. No error, no rollback.

A default route (`0.0.0.0/0`) classifies **HIGH** for exactly this reason, so it
waits for a reviewer rather than applying on merge.

---

## What happens after you merge

**Low-risk changes apply automatically. Higher-risk ones wait for a human.**

The classifier tiers your change — `LOW`, `HIGH` or `CRITICAL` — and **the tier
decides who has to approve it**. You never pick the tier, and neither does
anyone else; it is computed from what you changed.

* **LOW** — applies as soon as your PR merges. No approval, no waiting.
* **HIGH or CRITICAL** — the run pauses at *"Waiting for review"* until a named
  reviewer approves it. That is normal, not a stuck pipeline, and the approver's
  name is recorded in your change's evidence bundle.

A default route, a zone removal or a removed `deny` will hold for a reviewer. If
you want to know before you open the PR, the `classify` check on the PR reports
the tier of every change.

Once applied, the change is pushed to SCM and reaches the firewall within about a
minute.

---

## Finding out why traffic is allowed

`fwgitops where` answers the question an incident starts with: **which request
authorised this?**

```
$ fwgitops where 10.20.1.55

RULES — what permits or denies it
  AccessRequest  REQ-2026-0725  (prod-edge)
      matched : address_objects[0].value = 10.20.1.0/24
      why     : 10.20.1.0/24 contains 10.20.1.55
      ticket  : JIRA-20725  (martono@corp, 2026-07-25)
      intent  : intent/prod/observability/REQ-2026-0725.yaml
      evidence: evidence/prod-edge/REQ-2026-0725.json

ROUTES — what carries it
  RouteRequest  REQ-2026-0803  (prod-edge)   <- CARRIES IT
```

It matches by **containment**, which is why `grep` is not a substitute: your log
line holds a host (`10.20.1.55`), the intent holds a range (`10.20.1.0/24`), and
grep finds nothing — which reads exactly like "no rule permits this".

It also takes a CIDR, a zone or app name, a request id, a **ticket**, or a
requester. `--json` for piping into an incident timeline.

If nothing matches, that is an answer, not an error: no request in this
repository authorised it, and `fwgitops drift` is the tool for config that
arrived some other way.

---

## Where things live

- **Requests:** `intent/<environment>/<team>/REQ-*.yaml`
- **Valid environments:** `catalog/environments.yaml`
- **Named services / apps / App-IDs / profiles:** `catalog/*.yaml`
- **What each field maps to on the firewall:** `docs/adr/0003-security-rule-component-model.md`

If a name you need (a service, app, profile, or App-ID) isn't in the catalogs,
open a PR adding it to the relevant `catalog/*.yaml` — those are platform-
maintained lists, and adding to them is a reviewed change just like a rule request.
