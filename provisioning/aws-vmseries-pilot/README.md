# aws-vmseries-pilot — Day-1 VM-Series firewall (BYOL → SCM)

Stands up ONE VM-Series firewall in AWS (ap-southeast-1), bootstraps it, and
onboards it to Strata Cloud Manager in the `GitOps` folder. This is the pilot
device that unblocks **finding #12** (a folder push needs a bound device).

`terraform validate` passes against Palo's `swfw-modules` 2.2.7. **Applying costs
money** (EC2, BYOL) — the plan is stand up → onboard → confirm push → destroy.

## Prerequisites (you provide)

- [ ] **AWS creds** for account 162504351755 (`aws sts get-caller-identity`).
- [ ] **Marketplace subscription** to the VM-Series **BYOL** AMI — accept terms in
      the AWS console, then copy the **AMI id for ap-southeast-1**.
- [ ] **BYOL auth code** (VM-Series auth code / NGFW credits) from Palo.
- [ ] **SCM auto-registration PIN** (id + value) — SCM → Device onboarding. **Time-limited**,
      so generate it right before `apply`.
- [ ] An **EC2 key pair** in ap-southeast-1 (`aws ec2 create-key-pair ...` or reuse one).
- [ ] Your public IP for `mgmt_allowed_cidr` (`curl ifconfig.me`).

## Run

```bash
cp terraform.tfvars.example terraform.tfvars   # fill it in (gitignored)

../../terraform/make-backend.sh provisioning/aws-vmseries-pilot   # writes backend.hcl
terraform init -backend-config=backend.hcl
terraform plan      # review — expect ~1 instance, 1 VPC, subnets, S3, IAM
terraform apply
terraform output    # mgmt_public_ip + next steps
```

## Verify onboarding

1. Wait ~10-15 min (boot + bootstrap + registration).
2. SCM → Device onboarding: the firewall should appear in folder `GitOps`.
3. Retry the folder push (finding #12) — now it has a target. Job should run.

## Tear down (do this when done — it's billing hourly)

```bash
terraform destroy
```

`force_destroy = true` on the bootstrap bucket lets destroy remove it cleanly.

## Notes

- eth0 = mgmt (public IP, your-IP-locked SG); eth1 = dataplane. No mgmt-interface-swap.
- The init-cfg + auth code live in the S3 bootstrap bucket (private, encrypted),
  read by the instance profile. `terraform.tfvars` (secrets) is gitignored.
- Deprecation warnings on `validate` come from Palo's module, not this config.
