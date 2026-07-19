# provisioning/terraform — VM-Series instantiate (per cloud) + SCM baseline

Separate submodules per cloud (bootstrap mechanics differ enough that a cloud-agnostic
abstraction costs more than it saves). Both feed the shared SCM onboard+baseline module.

- `aws/` — EC2 VM-Series: init-cfg via user-data or S3 bootstrap bucket + IAM role,
  VPC/subnets/SGs, mgmt + dataplane ENIs.
- `gcp/` — GCE VM-Series: init-cfg via instance metadata or GCS bootstrap bucket + service
  account, VPC/firewall-rules, mgmt + dataplane NICs.

Skeleton builds ONE cloud first (AWS-first if it's a coin flip), then the second front door.
PA-series (hardware) has no Terraform instantiate — it enters via ZTP (see ../bootstrap).
