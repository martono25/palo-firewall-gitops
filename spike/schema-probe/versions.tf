# Isolated probe for Part A of the scm spike. Deliberately has NO version
# constraint, NO backend, and NO provider config — so `terraform init` cannot
# fail on our (still-unverified) version pin, and the schema dump needs no
# credentials and touches no tenant.
#
#   cd spike/schema-probe
#   terraform init
#   terraform providers schema -json > ../schema.json
#   cd ../.. && ./spike/schema-answers.sh spike/schema.json
#
# Record the resolved version from `terraform version` output, then pin it in
# terraform/modules/security_folder/versions.tf and terraform/prod-edge/main.tf.
terraform {
  required_providers {
    scm = {
      source = "PaloAltoNetworks/scm"
    }
  }
}
