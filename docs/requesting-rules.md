# Requesting a firewall rule

This is the day-to-day guide for **requesting a firewall rule change**. You do
not touch the firewall, SCM, or Terraform. You describe *what you need* in a small
YAML file, open a pull request, and the platform validates it, shows you the risk
and the exact change, and — once approved — applies it to the firewall for you.

You do **not** need to know Terraform, the SCM API, or PAN-OS internals to use this.

---

## Before you start (prerequisites)

**The easy path (website only) needs almost nothing:**
- A **GitHub account** that has been granted access to the
  `martono25/palo-firewall-gitops` repository (ask the platform team to add you).
- A web browser.

That's it — if you use **Option A (GitHub website)** below, you don't install
anything.

**Only if you prefer the command line (Option B),** install these one time:
- **Git** — macOS: `xcode-select --install` (or `brew install git`);
  Windows: <https://git-scm.com/download/win>; Linux: `sudo apt install git`.
- **(optional) GitHub CLI** `gh` for the `gh pr create` shortcut —
  <https://cli.github.com/>, then `gh auth login`.
- **Clone the repo once** (this is the step that was missing if you saw
  *"fatal: not a git repository"* — you must be *inside* the cloned folder):
  ```bash
  git clone https://github.com/martono25/palo-firewall-gitops.git
  cd palo-firewall-gitops
  ```

You do **not** need Terraform, the SCM/AWS credentials, Python, or `fwgitops`
installed to request a rule — those are for the platform operator, not you.

---

## The workflow (5 steps)

1. **Add an intent file** under `intent/<environment>/<app-or-team>/`, named after
   your request id, e.g. `intent/prod/payments/REQ-2026-0142.yaml`.
2. **Open a pull request.** CI (`pr-validate`) automatically:
   - validates your request (and tells you exactly what to fix if it's wrong),
   - classifies its **risk** (LOW / HIGH / CRITICAL),
   - shows the **enrich preview** (App-ID, profile, log-forwarding, ordering) and
     the **Terraform plan** as a PR comment.
3. **Get it reviewed.** A reviewer reads the risk + preview and approves.
4. **Merge.** On merge, the rule is applied to the firewall automatically.
   LOW-risk changes auto-apply; HIGH/CRITICAL require an explicit approval step.
5. **Done.** The rule is live. An evidence record (who/why/what/when) is stored
   automatically.

---

## How to open the pull request

You do not need the command line. Two ways:

### A. GitHub website (easiest)

1. Go to the repo: **github.com/martono25/palo-firewall-gitops**
2. Click **Add file → Create new file**.
3. In the filename box, type the full path — typing `/` makes folders:
   `intent/prod/payments/REQ-2026-0142.yaml`
4. Paste your intent YAML into the editor.
5. Click **Commit changes…**, choose **"Create a new branch for this commit and
   start a pull request"**, then **Propose changes**.
6. Give the PR a title (e.g. *"Request REQ-2026-0142: web → payments"*) and click
   **Create pull request**.
7. Watch the automated checks + the PR comment (validation, risk, preview).

### B. Command line

Requires the one-time setup above (Git installed + repo cloned). **Run every
command from *inside* the cloned `palo-firewall-gitops` folder** — running from
your home directory is what causes `fatal: not a git repository`.

```bash
# 0. one-time: clone + enter the repo (skip if you already have it)
git clone https://github.com/martono25/palo-firewall-gitops.git
cd palo-firewall-gitops

# 1. make sure you're on the latest main, then branch for your request
git checkout main
git pull
git checkout -b req/REQ-2026-0142

# 2. create your intent file
mkdir -p intent/prod/payments
$EDITOR intent/prod/payments/REQ-2026-0142.yaml   # paste your request

# 3. commit, push, open the PR
git add intent/prod/payments/REQ-2026-0142.yaml
git commit -m "request: REQ-2026-0142 web -> payments"
git push -u origin req/REQ-2026-0142
gh pr create --fill        # or open the PR link that `git push` prints
```

Either way: the PR runs validation automatically. If it fails, read the message,
fix the file, and push again (web: edit the file on your branch and commit; CLI:
edit the file, then `git add … && git commit --amend --no-edit && git push -f`).

---

## Quick start — a minimal request

```yaml
# intent/prod/payments/REQ-2026-0142.yaml
apiVersion: fw-intent/v1
kind: AccessRequest
metadata:
  id: REQ-2026-0142            # your request id (also the rule name)
  requester: jane.doe@corp     # you
  ticket: JIRA-4821            # the change ticket (audit linkage)
  justification: "Web tier needs to reach the payments API"
  requested: "2026-08-01"
spec:
  environment: prod            # which firewall/folder (see catalog/environments.yaml)
  action: allow
  source: [{cidr: "10.20.1.0/24"}]
  destination: [{cidr: "10.20.9.10/32"}]
  service: [{protocol: tcp, port: "443"}]
```

That is a valid request. Everything below is optional and adds precision
(App-ID, inspection, logging, user/URL matching, ordering, …).

---

## Full example — an inspected, App-ID rule

```yaml
apiVersion: fw-intent/v1
kind: AccessRequest
metadata:
  id: REQ-2026-0143
  requester: jane.doe@corp
  ticket: JIRA-4830
  justification: "Web tier to payments API, HTTPS only, inspected"
  requested: "2026-08-01"
  expires: "2026-11-01"          # optional: mark when this should be reviewed/removed
spec:
  environment: prod
  action: allow
  source: [{cidr: "10.20.1.0/24"}]
  destination: [{fqdn: "payments.internal"}]
  service: [{protocol: tcp, port: "443"}]
  application: ["ssl", "web-browsing"]   # App-ID (not just the port)
  profile: best-practice                 # security profile group -> threat inspection
  log_forwarding: log-best               # forward logs to the SIEM/data lake
  log_start: true
  description: "web -> payments (inspected)"
  position: top                          # place at the top of the rulebase
```

---

## Field reference

### `metadata` (all required except `expires`)

| Field | Meaning |
|---|---|
| `id` | Your request id — becomes the rule name. Letters/digits/`.`/`_`/`-` only. |
| `requester` | Who is asking. |
| `ticket` | Change ticket id (audit linkage). Letters/digits/`.`/`_`/`-` only. |
| `justification` | Why — one line of business reason. |
| `requested` | Date requested, `YYYY-MM-DD`. |
| `expires` | *(optional)* Review/expiry date, `YYYY-MM-DD`. |

### `spec`

| Field | Required | Default | Meaning |
|---|:--:|---|---|
| `environment` | ✅ | — | Which firewall/folder. Valid values live in `catalog/environments.yaml`. |
| `action` | ✅ | — | `allow` · `deny` · `drop` · `reset-client` · `reset-server` · `reset-both`. |
| `source` | ✅ | — | List of endpoints (see **Endpoints**). |
| `destination` | ✅ | — | List of endpoints. |
| `service` | ✅ | — | List of services (see **Services**). |
| `application` | | `["any"]` | App-ID names, e.g. `["ssl","web-browsing"]`. Port-only if omitted. |
| `profile` | | *(none)* | Security profile **group** name → threat inspection. Omitted = **no inspection** (flagged, not blocked). |
| `log_forwarding` | | *(none)* | Log-forwarding profile name (send logs off-box). |
| `source_user` | | `["any"]` | User-ID users/groups. Reserved: `any`, `pre-login`, `known-user`, `unknown`. |
| `category` | | `["any"]` | URL categories to match, e.g. `["financial-services"]`. |
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
- {app: "payments-api"}       # a named app from catalog/apps.yaml (carries its own zone)
```

### Services

```yaml
- {protocol: tcp, port: "443"}         # a port
- {protocol: udp, port: "53"}
- {protocol: tcp, port: "8000-8100"}   # a range
- {name: "https"}                       # a named service from catalog/services.yaml
```

---

## More examples

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

**Allow HTTPS but only to financial URLs, place after another rule:**
```yaml
spec:
  environment: prod
  action: allow
  source: [{cidr: "10.20.5.0/24"}]
  destination: [{cidr: "0.0.0.0/0"}]
  service: [{protocol: tcp, port: "443"}]
  category: ["financial-services"]
  position: "after:REQ-2026-0142"
```

---

## Reading the PR feedback

When you open the PR, CI comments with three things:

- **Validation** — if your request is malformed, the job fails with a line-by-line
  message (`spec.action: must be one of [...]`). Fix and push again.
- **Risk tier** — each change is classified:
  - **LOW** — auto-applies on merge.
  - **HIGH / CRITICAL** — needs an explicit approval step; will **not** auto-apply.
    Common triggers: broad source/destination, `0.0.0.0/0` any-any, exposing risky
    ports from the internet, a negated match, a brand-new zone path, or an
    uninspected allow.
- **Enrich preview + plan** — the exact fields that will be set (App-ID, profile,
  log-forwarding, ordering) and the Terraform plan for the objects/skeleton. This
  is what a reviewer approves against.

> **Note:** an `allow` with no `profile` is flagged (`allow_without_inspection`) —
> it's permitted, but it means the traffic is allowed **without threat inspection**.
> Add a `profile` unless you deliberately want an uninspected allow.

---

## Common mistakes

| Message / symptom | Fix |
|---|---|
| `spec.action: must be one of [...]` | Use a valid action (see the list above). |
| `invalid CIDR ... (host bits set …)` | Use the network address (`10.20.1.0/24`, not `10.20.1.5/24`). |
| `unknown environment 'staging'; known: [...]` | Use an environment from `catalog/environments.yaml`. |
| `unknown App-ID 'ssll'; known: [...]` | Fix the App-ID name (see `catalog/applications.yaml`). |
| `unknown security profile group '…'` | Use a profile group that exists (see `catalog/profiles.yaml`). |
| Change is **HIGH/CRITICAL** and won't auto-apply | Expected for broad/any-any/exposed/negated rules — get the explicit approval, or tighten the rule. |
| `rules.auto.tfvars.json is stale` | You edited an intent but didn't recompile — run `fwgitops compile intent --out terraform` and commit, or just let CI regenerate it. |

---

## Where things live

- **Your requests:** `intent/<environment>/<team>/REQ-*.yaml`
- **Valid environments:** `catalog/environments.yaml`
- **Named services / apps / App-IDs / profiles:** `catalog/*.yaml`
- **What each field maps to on the firewall:** `docs/adr/0003-security-rule-component-model.md`

If a name you need (a service, app, profile, or App-ID) isn't in the catalogs,
open a PR adding it — those are platform-maintained lists and adding to them is a
reviewed change, just like a rule request.
