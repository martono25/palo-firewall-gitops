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

## Before you start

- A **GitHub account** that has been added to the `martono25/palo-firewall-gitops`
  repository. If you can't open <https://github.com/martono25/palo-firewall-gitops>,
  ask the platform team to grant you access. **This is the only prerequisite for
  the website path.**
- Decide your request's **id** (the rule name), e.g. `REQ-2026-0142`. It must be
  unique (no existing rule with that id) and use only letters, digits, `.`, `_`,
  `-`. Convention is `REQ-<year>-<number>`, but any unique id works.

---

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

### Step 7. It goes live automatically (~2 minutes)

Merging kicks off the **apply** automatically. To watch it:

- Go to the **Actions** tab → the **apply** workflow → the run named after your
  merge. Green ✅ = your rule is now applied to the firewall.

That's it. Your rule is live, and an evidence record (who / why / what / when) was
stored automatically. If the traffic is to a firewall with a live device, the rule
reaches the device shortly after (a brand-new device can take 20–30 min on its
first sync).

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

---

## Field reference

### `metadata` (all required except `expires`)

| Field | Meaning |
|---|---|
| `id` | Your request id — becomes the rule name. Letters/digits/`.`/`_`/`-` only, unique. |
| `requester` | Your email. |
| `ticket` | Change ticket id (audit linkage). Same character rules as `id`. |
| `justification` | One line: why you need this. |
| `requested` | Date requested, `YYYY-MM-DD`. |
| `expires` | *(optional)* Review/removal date, `YYYY-MM-DD`. |

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
| `position` | | `bottom` | Ordering: `top` · `bottom` · `before:<rule-id>` · `after:<rule-id>`. |

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
```

---

## Examples

**Inspected HTTPS allow (App-ID + profile + logging):**
```yaml
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
spec:
  environment: prod
  action: drop
  source: [{cidr: "10.20.2.0/24"}]
  destination: [{cidr: "10.20.9.11/32"}]
  service: [{protocol: tcp, port: "23"}]
```

**Allow a specific user group only:**
```yaml
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
| Change is **HIGH/CRITICAL** and won't auto-apply | Expected for broad/any-any/exposed/negated rules — get the explicit approval, or tighten the rule. |

---

## Where things live

- **Requests:** `intent/<environment>/<team>/REQ-*.yaml`
- **Valid environments:** `catalog/environments.yaml`
- **Named services / apps / App-IDs / profiles:** `catalog/*.yaml`
- **What each field maps to on the firewall:** `docs/adr/0003-security-rule-component-model.md`

If a name you need (a service, app, profile, or App-ID) isn't in the catalogs,
open a PR adding it to the relevant `catalog/*.yaml` — those are platform-
maintained lists, and adding to them is a reviewed change just like a rule request.
