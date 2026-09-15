# Changing the AWS account

*How-to — moving the platform to a new AWS account, end to end. For what each
command does in detail, see [cli-reference.md](cli-reference.md); for day-to-day
operation, [operator-runbook.md](operator-runbook.md).*

The pilot's AWS account is a **time-limited subscription** (about five weeks).
When it ends, the account ID changes, and three things go with the old one: the
Terraform state bucket, the CI role GitHub signs in with, and the firewall VM.

This guide is the procedure that brought the platform back on 2026-09-14/15,
with every step that was manual then either scripted or written down here.

**Do it before the expiry date if you can.** The first time, nothing noticed for
17 days: every nightly run was red, and red was the only signal.

---

## What changes, and what survives

| | On an account change |
|---|---|
| **SCM policy** — rules, zones, routes, objects | **survives.** It lives in Strata Cloud Manager, not AWS |
| **Git** — intents, catalog, evidence | survives |
| Terraform state bucket `fw-gitops-tfstate-<account>` | **lost** — recreated empty, then rebuilt by import |
| CI OIDC role + `AWS_OIDC_ROLE_ARN` | **lost** — recreated, variable re-pointed |
| Firewall VM (and its serial) | **lost** — a new VM gets a **new serial** |
| EC2 key pair, Marketplace subscription, vCPU quota | per account — **redo** |
| SCM registration PIN | survives — valid to **2027-03-15** |
| BYOL licence | survives — expires **2026-11-03** |

---

## Before you start

You need all of these in hand. None of them can be scripted.

- [ ] **The new AWS subscription**, and an access key for it
- [ ] **Two change tickets:**
  - one to **retire** the old firewall's intents (e.g. JIRA-9400 last time)
  - one to **onboard** the new firewall (e.g. JIRA-9500)

  They must be new tickets. A removal quoting the ticket that *created* an
  object makes its evidence name the wrong authorisation.
- [ ] `gh` signed in to this repository, and `~/.fwgitops/scm.env` present
- [ ] **The old firewall deleted from CSP** (Assets → Devices). That removes it
  from SCM, so Step 3 skips its root cleanly instead of importing a dead device
- [ ] The repo on `main`, up to date, with `.venv` installed (`pip install -e .`)

> **Never paste an AWS key into a chat, ticket or commit.** Enter it only at
> the `aws configure` prompt. If it has been pasted anywhere, rotate it.

---

## Step 1 — Rehearse, while the old account still works

```bash
./terraform/rebootstrap-account.sh --rehearse
```

This rebuilds state against **live SCM** using empty scratch state, and writes
nothing real. You want:

```
   ok  GitOps: Plan: 1 to import, 0 to add, 1 to change, 0 to destroy.
   ok  device-...: Plan: 3 to import, 0 to add, 0 to change, 0 to destroy.
   ok  prod-edge: Plan: 9 to import, 0 to add, 6 to change, 0 to destroy.
── rehearsal PASSED
```

`to change` is expected: an import cannot carry a rule's `position`. **Any
`to add` or `to destroy` is a failure** — fix it before the account ends, while
you still have state to compare against.

The counts grow as intents are added; the zeros must not.

---

## Step 2 — Sign in to the new account

```bash
aws configure
```

```bash
aws sts get-caller-identity --query Account --output text
```

Confirm the number is the **new** account before going further. Everything
after this writes to whichever account you are signed in to.

---

## Step 3 — Run the re-bootstrap

```bash
./terraform/rebootstrap-account.sh
```

It stops at each of the two applies for you to type `yes`:

| Step | Creates | You check |
|---|---|---|
| 3. state bucket | `fw-gitops-tfstate-<new account>` — versioned, encrypted, TLS-only, public access blocked | plan is 5 to add |
| 4. CI role | GitHub OIDC provider + `fwgitops-ci-*` role, scoped to this repo and the state bucket | plan is 3 to add |

Then, without prompting: re-points `AWS_OIDC_ROLE_ARN`, rewrites every
`backend.hcl`, compiles, and writes `terraform/<root>/imports_recovery.tf` —
**refusing unless every plan adds nothing and destroys nothing.**

**Expect the old firewall's root to be skipped:**

```
device-<old serial>   SKIPPED — firewall <old serial> is not registered in SCM. Retire it: ...
```

That is correct. A dead firewall is retired (Step 4), not imported.

**If it stops**, read the line above the error, fix that, and run it again. Every
step detects what is already done.

> The script's fresh-account path — actually *creating* the bucket and role, and
> importing an OIDC provider the account already has — has not run yet. It was
> rehearsed and run as a no-op in account 475369997213 on 2026-09-15. If step 3
> or 4 fails on a fresh account, the Terraform error names the resource.

---

## Step 4 — One PR: re-import state and retire the old firewall

These go in **one** pull request. Merging `terraform/**`, `catalog/**` or
`intent/**` triggers an apply, and an apply before the imports land would try to
create every rule again.

**The old firewall must be retired in the same PR** — `verify-catalog` rejects
every run while the catalog declares a serial SCM no longer has.

```bash
git checkout -b fix/move-to-<new account>
git add terraform/*/imports_recovery.tf
```

Retire the old firewall — `<old>` is its serial:

```bash
git rm intent/prod/edge-fw-<last4 of old>/*.yaml
git rm -r terraform/device-<old>
```

In `catalog/folders.yaml`, delete the `"<old>":` entry under `prod-edge` →
`devices:` (leave `devices: {}` if it was the only one). In
`catalog/interfaces.yaml`, delete each `"<old>": ethernet1/N` line.

Check before committing:

```bash
fwgitops compile intent && fwgitops verify-catalog
```

```bash
python -m pytest -q
```

Open the PR. **The `Removes:` lines go in the PR body** — a squash merge lands the
body on `main`, and that is what the apply reads. One line per deleted intent,
on the **retirement** ticket:

```
Removes: REQ-2026-0910 (JIRA-XXXX)
Removes: REQ-2026-0911 (JIRA-XXXX)
Removes: REQ-2026-0912 (JIRA-XXXX)
```

`pr-validate` reads the body **at push time**. If you edit the body after
pushing, push again (an empty amend will do) or the check reads the old one.

---

## Step 5 — Merge, approve, clean up

Removing interface config classifies **HIGH**, so the apply waits at the
`firewall-apply` gate: **Actions → apply → the waiting run → Review deployments →
Approve and deploy.**

Confirm the apply log shows, per folder:

```
Apply complete! Resources: N imported, 0 added, M changed, 0 destroyed.
```

Then delete the import blocks in a follow-up PR — they are no-ops once imported,
and have no reason to stay:

```bash
git rm terraform/*/imports_recovery.tf
```

**At this point enforcement is back** for rules and objects, with no firewall
behind it. `device-sync` passes because the catalog declares no firewall and SCM
holds none. Dispatch drift-detect and read it — it reports what changed while
nothing was checking:

```bash
gh workflow run drift-detect.yml
```

---

## Step 6 — Prepare the new account for the firewall

Four things only you can do, each once per account, all in **ap-southeast-1**:

1. **Key pair.** EC2 → Key pairs → Create, named **`fwgitops-pilot`**, RSA,
   `.pem`. Move it to the repo root and lock it down:

   ```bash
   chmod 600 fwgitops-pilot.pem
   ```

   Delete the download copy. `*.pem` is gitignored; keep it that way.

2. **Marketplace.** Subscribe to the **VM-Series BYOL** listing. Without it, the
   launch fails. Check without launching anything:

   ```bash
   aws ec2 run-instances --dry-run --region ap-southeast-1 --instance-type m5.xlarge \
     --image-id "$(awk -F'"' '/^vmseries_ami_id/{print $2}' provisioning/aws-vmseries-pilot/terraform.tfvars)"
   ```

   Want: `Request would have succeeded, but DryRun flag is set.`

3. **vCPU quota.** New accounts start at **5** vCPU. The firewall needs **4**, so
   it fits. The optional traffic-test hosts need 4 more — request **16** under
   Service Quotas → EC2 → *Running On-Demand Standard instances* if you want them.

4. **Management IP.** `mgmt_allowed_cidr` in
   `provisioning/aws-vmseries-pilot/terraform.tfvars` must be your current
   egress `/32`. **Never widen it** — the first pilot took 106 brute-force logins
   in a week with SSH open to the internet.

   ```bash
   curl -s ifconfig.me
   ```

The step 3 script already wrote the pilot's `backend.hcl`. The PIN and auth code
in `terraform.tfvars` carry over.

---

## Step 7 — Launch the firewall

```bash
cd provisioning/aws-vmseries-pilot
terraform init -reconfigure -backend-config=backend.hcl
terraform plan -out=pilot.tfplan
```

Read the plan before applying. You want:

- **33 to add, 0 to change, 0 to destroy**
- `metadata_options { http_tokens = "required" }` — IMDSv2 (see `firewall.tf`)
- `aws_security_group.mgmt` ingress on 443 and 22 **from your `/32` only**

```bash
terraform apply pilot.tfplan
```

---

## Step 8 — Wait, then verify on the device

Boot, bootstrap, licensing and SCM registration take **10–15 minutes**. Watch
the console for the licence — this is also where an IMDSv2 problem would show.
**Run it during first boot:** `--latest` returns only recent console output, and
on a firewall that has been up for hours the bootstrap lines have scrolled away.

```bash
aws ec2 get-console-output --region ap-southeast-1 --latest --output text \
  --instance-id "$(terraform output -raw instance_id)" | grep -iE "bootstrap|installed license"
```

Want: `Bootstrap media sanity check passed` and `Successfully installed license
key using authcode`. If licensing fails, relax IMDSv2 without relaunching:

```bash
aws ec2 modify-instance-metadata-options --region ap-southeast-1 --http-tokens optional \
  --instance-id "$(terraform output -raw instance_id)"
```

Then on the device — **SCM showing it is not proof**:

```bash
printf 'set cli pager off\nshow system info\n' \
  | ssh -T -i ../../fwgitops-pilot.pem admin@"$(terraform output -raw mgmt_public_ip)" \
  | grep -E "^(serial|vm-license|sw-version|device-certificate-status):"
```

Want `vm-license: VM-SERIES-4` and `device-certificate-status: Valid`. **Note the
serial** — every step below uses it.

At this moment `verify-catalog` **rejects** (a firewall the catalog does not
declare) and `device-sync` reports it **out of sync**. Both are correct until
Step 9 lands.

---

## Step 9 — Adopt the new firewall

From the repo root, on a new branch. `<new>` is the serial from Step 8.

Name it in SCM, then write it into the repository from what SCM holds:

```bash
fwgitops onboard <new> --folder prod-edge --name fw-prod-edge-<last4>
```

```bash
fwgitops adopt-device <new> --folder prod-edge --ticket <ONBOARD-TICKET> --check
fwgitops adopt-device <new> --folder prod-edge --ticket <ONBOARD-TICKET>
```

`--check` must list **both** `catalog/folders.yaml` and `catalog/interfaces.yaml`
under `would write`. If it writes nothing but prints OK, stop — that was a real
defect, fixed on 2026-09-15, and the command now exits 3 instead.

Initialise the new device root and **commit its lock file**:

```bash
./terraform/make-backend.sh device-<new>
terraform -chdir=terraform/device-<new> init -backend-config=backend.hcl
```

**Write three interface intents** in `intent/prod/edge-fw-<last4>/`, one per
role. The addresses must be the private IPs AWS gave **this** VM's ENIs — the
old firewall's addresses will not work:

```bash
aws ec2 describe-network-interfaces --region ap-southeast-1 \
  --filters Name=attachment.instance-id,Values=<instance id> \
  --query 'NetworkInterfaces[].[Attachment.DeviceIndex,PrivateIpAddress]' --output text | sort -n
```

| Device index | Port | Role | Subnet |
|---|---|---|---|
| 1 | ethernet1/1 | `local` | 10.100.3.0/24 |
| 2 | ethernet1/2 | `internet` | 10.100.2.0/24 |
| 3 | ethernet1/3 | `dmz` | 10.100.1.0/24 |

Copy the shape of an existing InterfaceRequest from Git history, with a **new**
`id`, the onboarding `ticket`, and `spec.device: "<new>"`. Use ids never used
before — an evidence bundle is keyed by id.

Check, then open the PR:

```bash
fwgitops compile intent && fwgitops verify-catalog && python -m pytest -q
```

Addressing new interfaces classifies **LOW**: merging **applies and pushes with
no reviewer**. That is the platform's policy, not an oversight.

---

## Step 10 — Verify the firewall is enforcing

A green apply is not proof. On the device:

```bash
printf 'set cli pager off\nshow interface logical\nshow running security-policy\nshow advanced-routing route\n' \
  | ssh -T -i fwgitops-pilot.pem admin@<mgmt ip> \
  | grep -E '^ethernet1/|^"REQ-|^0\.0\.0\.0/0'
```

You want:

- **three interfaces**, each with its ENI address and zone
- **every `REQ-` rule** in deployment order — the device-scope push also delivers
  the inherited folder config (verified 2026-09-15)
- **a default route** via `10.100.2.1`

Then the platform's own checks:

```bash
fwgitops device-sync
fwgitops verify-catalog
gh workflow run drift-detect.yml
```

`device-sync` may print `first-push-pending`. That note is harmless — see the
[runbook](operator-runbook.md#first-push-pending-on-a-new-firewall-is-not-a-problem).

**Done** when drift-detect is green and reports `No drift of any kind`.

---

## If an apply fails with `403 Access denied`

Once, on 2026-09-15, a plan read of the logical router returned `403 Forbidden
{"msg": "Access denied"}` and succeeded seconds later with the same credentials.
Re-run the **workflow** — never `terraform apply` locally:

```bash
gh workflow run apply.yml
```

If it repeats, it is not transient: stop and investigate.

---

## Dates to keep

| What | Expires | Consequence |
|---|---|---|
| AWS account 475369997213 | ~2026-10-19 | this whole procedure |
| BYOL licence and subscriptions | 2026-11-03 | renew in CSP first — confirm with Palo Alto what an expired PA-VM term licence does before relying on it |
| SCM registration PIN | 2027-03-15 | a new VM cannot register — generate a new PIN in CSP |
| `AUTOMATION_PR_TOKEN` | ~2026-11-08 | evidence PRs stop merging — [runbook](operator-runbook.md#automation_pr_token-expired) |

## Related

- [operator-runbook.md § The AWS account expired](operator-runbook.md#the-aws-account-expired) — the short version
- [provisioning.md](provisioning.md) — the VM in depth: sizing, bootstrap, teardown
- [cli-reference.md](cli-reference.md#recover-state) — `recover-state`, `adopt-device`
- [removing-things.md](removing-things.md) — the `Removes:` contract
