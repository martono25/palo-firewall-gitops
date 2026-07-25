output "state_bucket" {
  description = "Name of the S3 bucket holding Terraform state. Use in each folder's backend.hcl."
  value       = aws_s3_bucket.tfstate.id
}

output "region" {
  description = "Region the state bucket lives in."
  value       = var.region
}

output "backend_hcl" {
  description = "Ready-to-use backend config lines for a per-folder root."
  value       = <<-EOT
    bucket       = "${aws_s3_bucket.tfstate.id}"
    region       = "${var.region}"
    key          = "<FOLDER>/terraform.tfstate"
    encrypt      = true
    use_lockfile = true
  EOT
}
