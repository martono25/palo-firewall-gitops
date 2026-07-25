#!/usr/bin/env bash
# Generate a per-folder backend.hcl from the LIVE AWS account — no placeholders.
# Usage (from repo root or terraform/):  ./terraform/make-backend.sh prod-edge [region]
set -euo pipefail
folder="${1:?usage: make-backend.sh <folder-dir> [region]}"
region="${2:-ap-southeast-1}"
acct=$(aws sts get-caller-identity --query Account --output text)
bucket="fw-gitops-tfstate-${acct}"
dir="terraform/${folder}"; [ -d "$dir" ] || dir="$folder"   # tolerate either cwd
cat > "${dir}/backend.hcl" <<HCL
bucket       = "${bucket}"
region       = "${region}"
key          = "${folder##*/}/terraform.tfstate"
encrypt      = true
use_lockfile = true
HCL
echo "wrote ${dir}/backend.hcl  (bucket=${bucket})"
echo "next: (cd ${dir} && terraform init -backend-config=backend.hcl)"
