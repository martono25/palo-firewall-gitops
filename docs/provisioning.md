# Provisioning a firewall (operator guide)

**Audience: platform operator** — the person who stands up firewalls. This is
**not** the rule-request flow (that's [`requesting-rules.md`](requesting-rules.md),
and needs no cloud/licensing access). Provisioning stands up the VM-Series itself
so that rule requests have somewhere to land.

> **This page gets a firewall to exist and reach SCM. It does not configure it.**
> Interfaces, zones and routes come from Git afterwards — that chain is built and
> proven on hardware, and it is
> [`building-a-folder.md`](building-a-folder.md)
> ([ADR-0002](adr/0002-day1-provisioning-thin-bootstrap.md)). The split is
> deliberate: bootstrap is a `terraform apply` with licensing secrets, everything
> after it is a reviewed pull request.

**Read these in order.** Each hands off to the next, and a new operator should not
need anything outside them:

| Step | Guide |
|---|---|
| 1. Stand up the firewall | this page |
| 2. Configure it — interfaces, zones, routes | [`building-a-folder.md`](building-a-folder.md) |
| 3. Add a rule | [`requesting-rules.md`](requesting-rules.md) |
| 4. Run it day to day | [`operator-runbook.md`](operator-runbook.md) |

**Replacing a firewall that already exists?** Do not start here. The serial is
threaded through the catalog, the intents and a Terraform root, and the ordering
matters — [`operator-runbook.md` § Replacing a firewall](operator-runbook.md#replacing-a-firewall-new-serial)
sequences it and sends you back here at the right moment.

---

## What it does

`provisioning/aws-vmseries-pilot` (Terraform) stands up, on AWS:
VPC + mgmt/dataplane subnets, a VM-Series instance, an EIP, and a bootstrap S3
bucket (init-cfg + license authcode). On first boot the firewall:
licenses itself (BYOL), fetches its device certificate (registration PIN),
connects to SCM, and **auto-onboards** into the target folder via a serial-number
onboarding rule. After that, Day-2 rule requests flow to it.

---

## Prerequisites

### 1. Install the tools (one time, on your machine)

| Tool | macOS (Homebrew) | Other |
|---|---|---|
| **Git** | `brew install git` | <https://git-scm.com/downloads> |
| **Terraform** ≥ 1.6 | `brew install terraform` | <https://developer.hashicorp.com/terraform/install> |
| **AWS CLI** v2 | `brew install awscli` | <https://aws.amazon.com/cli/> |
| **Python** ≥ 3.11 | `brew install python@3.11` | <https://www.python.org/downloads/> |

Then clone the repo and install the `fwgitops` CLI:

```bash
git clone https://github.com/martono25/palo-firewall-gitops.git
cd palo-firewall-gitops
python3 -m venv .venv && source .venv/bin/activate
pip install -e .            # installs the `fwgitops` command
fwgitops --help             # verify
terraform version           # verify
```

### 2. Configure credentials

**AWS** (one time): `aws configure` (or `aws configure sso`) for access to the
target account/region. Verify: `aws sts get-caller-identity`.

**SCM** — the `fwgitops` commands that talk to SCM (`onboard`, `push`, `enrich`)
read the service-account credentials from **environment variables**, so they must
be present in **every shell** you run them from:

```bash
export SCM_CLIENT_ID=...
export SCM_CLIENT_SECRET=...   # keep out of shell history, e.g. `read -rs SCM_CLIENT_SECRET`
export SCM_SCOPE=tsg_id:XXXX
```

Convenient pattern — keep them in a **gitignored, mode-0600 file** and source it
per shell (this is what avoids retyping the secret):

```bash
# one time: create ~/.fwgitops/scm.env (chmod 600) containing the three exports
set -a; source ~/.fwgitops/scm.env; set +a   # run this in each new terminal
```

If you skip this, `fwgitops onboard/push/enrich` fail with a config/credentials
error.

### 3. Palo Alto / licensing inputs (operator-held)

| Need | Where it comes from |
|---|---|
| VM-Series **BYOL AMI** subscription | AWS Marketplace, same region |
| VM-Series **BYOL auth code** | Palo Alto CSP (Assets → licenses) |
| **Registration PIN** (id + value) | CSP → Products → Device Certificates → Generate Registration PIN (time-limited) |
| An existing **EC2 SSH key pair** in the region | your AWS |
| A **serial-number onboarding rule** in SCM | SCM UI, once — matches the device serial prefix → target folder |
| A **deployment profile sized to the vCPU count you intend** | Palo Alto CSP → flexible VM-Series licensing |

**Size the deployment profile BEFORE the firewall registers.** Under flexible
licensing the profile sets the vCPU allocation, and PAN-OS licenses itself to
match the instance on first boot — it does not stay capped at a smaller tier.
Registering a 16-vCPU instance against a profile you meant to be 4 vCPU consumes
roughly 4x the credits from that moment, and the credits cost more than the EC2
hours.

`instance_type` is the other half of the same decision and it has a hard floor:

| | |
|---|---|
| `m5.xlarge` | 4 vCPU, **4 ENIs** → mgmt + `ethernet1/1..1/3` |
| `m5.4xlarge` | 16 vCPU, 8 ENIs → `ethernet1/4` reachable |

A VM-Series interface exists only when an ENI sits at the matching device index,
and **every** 4-vCPU instance type in `ap-southeast-1` caps at 4 ENIs. So three
dataplane interfaces is the ceiling at 4 vCPU — which is what this deployment
uses. Needing a fourth means 16 vCPU and roughly 4x the licence credits, so it is
a decision to make deliberately rather than discover. See the note on
`instance_type` in `provisioning/aws-vmseries-pilot/variables.tf`.

All secret inputs go in `terraform.tfvars` (**gitignored — never committed**).

---

## One-time platform bootstrap (before the *first* firewall)

These exist **once per platform**, not per firewall. Skip this section if the
platform is already set up (state backend + SCM folder + onboarding rule exist).

1. **Terraform state backend** (S3 bucket for state) — `terraform/bootstrap-backend`
   (`terraform init && terraform apply`). One-off.
2. **SCM folder** the firewalls land in — `terraform/bootstrap-scm-folder`.
3. **CI OIDC role** (so GitHub Actions can reach the state backend, no stored keys)
   — `terraform/github-oidc`.
4. **`backend.hcl`** in each Terraform dir — this file is **gitignored** (points at
   the state bucket from step 1), so a fresh clone won't have it. Create it from
   the example, e.g.:
   ```bash
   cp terraform/prod-edge/backend.hcl.example terraform/prod-edge/backend.hcl
   $EDITOR terraform/prod-edge/backend.hcl        # set bucket/key/region
   # (CI generates this automatically via terraform/make-backend.sh)
   ```
5. **Serial-number onboarding rule** in the SCM UI — one rule that matches your
   VM-Series serial-number prefix → the target folder, so devices auto-place on
   registration. (Device-onboarding APIs are a separate privileged domain; this is
   a one-time UI step.)

---

## Steps (per firewall)

```bash
cd provisioning/aws-vmseries-pilot

# 0. create backend.hcl if it doesn't exist (gitignored S3 state config:
#    bucket / key / region — use terraform/prod-edge/backend.hcl.example as the format)
$EDITOR backend.hcl

# 1. Fill in your inputs (secrets stay local — this file is gitignored)
cp terraform.tfvars.example terraform.tfvars
$EDITOR terraform.tfvars     # ssh_key_name, mgmt_allowed_cidr, vmseries_ami_id,
                             # scm_folder, scm_registration_pin_id/value, vmseries_authcode

# 2. Stand it up (~2-3 min for the infra)
terraform init -backend-config=backend.hcl
terraform apply

# 3. Wait ~10-20 min: boot -> license -> register -> auto-onboard to the folder.
#    A fresh device also downloads content (App-ID/AV) before its first config push.
```

### Verify it came up

```bash
# On the device (SSH key = your EC2 key pair; pipe commands to skip the pager):
printf 'set cli pager off\nshow system info\nshow cloud-management-status\n' \
  | ssh -T -i <ec2-key>.pem admin@<mgmt_public_ip>
#   serial: 0079...            <- device licensed
#   device-certificate-status: Valid
#   Cloud Management: Connected: yes

# CHECK THE LICENCE TIER, not just that it licensed. It follows the INSTANCE,
# so a profile you meant to be 4 vCPU still licenses at VM-SERIES-16 if the
# instance is 16 vCPU — silently, and it bills that way from first boot.
printf 'set cli pager off\nshow system info\n' \
  | ssh -T -i <ec2-key>.pem admin@<mgmt_public_ip> | grep -E 'vm-license|vm-cap-tier'
#   vm-license: VM-SERIES-4    <- matches the deployment profile you sized

# In SCM (via the API), the device appears as {name:<serial>, parent:<folder>}:
fwgitops onboard <serial> --folder <scm_folder> --name fw-<folder>-<suffix>
#   verifies placement + sets a friendly display name
```

Once it shows in the folder and connected, it's ready — Day-2 rule requests to
that `environment` (which maps to the folder) will apply to it.

---

## Tear down

```bash
terraform destroy    # in provisioning/aws-vmseries-pilot
```

Two **manual CSP follow-ups** (there is no API for these):

1. **Delete the device in CSP** (Customer Support Portal). `terraform destroy`
   removes the AWS resources but the SCM/CSP device record persists. SCM can only
   *unassign* a device; **deletion is CSP-only**.
2. **This also frees the BYOL license seat.** `terraform destroy` does **not**
   deactivate the license, so each provision/destroy cycle leaks a seat until you
   delete the device in CSP. If you cycle VMs often and skip this, new VMs
   eventually fail to license (`serial: unknown`, *"device not found"*). Clean up
   stale CSP devices to reclaim seats.

### If you are rebuilding rather than retiring

Two more CSP steps, both **before** the new firewall boots:

3. **Resize the deployment profile** if the vCPU count is changing. Doing it
   after the firewall registers means it has already licensed at the old tier and
   started consuming at that rate.
4. **Generate a fresh registration PIN.** They are time-limited, so generate it
   immediately before `terraform apply`, not at the start of teardown. An expired
   PIN fails at device-certificate fetch, and the symptom is a firewall that
   boots, licenses, and never appears in SCM.

Then come back to [Steps (per firewall)](#steps-per-firewall) — and afterwards go
to [`operator-runbook.md` § Replacing a firewall](operator-runbook.md#replacing-a-firewall-new-serial)
for the repo side, because the new serial has to reach the catalog, the intents
and a Terraform root before anything will compile against it.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `zsh: command not found: fwgitops` (venv is active) | The package isn't installed in the venv. Run `pip install -e .` (from the repo root, venv active), then `rehash` (zsh) or open a new shell. |
| `fwgitops onboard/push/enrich` → config/credentials error | SCM env vars not loaded in this shell — run `set -a; source ~/.fwgitops/scm.env; set +a` first (see *Configure credentials*). |
| Device never appears in SCM; SSH shows `serial: unknown`, `device-certificate-status: None` | Registration/licensing failed — registration PIN expired/used up, or **no free license seats** (delete stale devices in CSP). |
| Device onboards but rules don't reach it for 20-30 min | Normal — a fresh VM finishes content bootstrap before its first config push. Verify on-device with `show running security-policy`, not just the SCM `is_first_push_done` flag (it lags). |
| `mgmt` unreachable | `mgmt_allowed_cidr` doesn't include your IP; or the device is still booting. |
| Device lands in "Available Devices", not the folder | The serial-number onboarding rule didn't match — check the rule's serial regex vs the device serial in SCM. |

---

## Provisioning vs requesting — who does what

| | Provisioning (this doc) | Requesting rules ([that doc](requesting-rules.md)) |
|---|---|---|
| Who | Platform operator | Any engineer |
| Needs | AWS + CSP + licensing access | Just write a YAML + open a PR |
| Frequency | Once per firewall | Every rule change |
| Interface | `terraform apply` (manual, v2.0 = GitOps) | Pull request |
