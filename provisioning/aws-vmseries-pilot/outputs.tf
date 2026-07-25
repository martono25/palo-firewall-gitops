output "mgmt_public_ip" {
  description = "Firewall management public IP — browse to https://<ip> once booted (~10-15 min)."
  value       = try(module.vmseries.public_ips["mgmt"], null)
}

output "instance_id" {
  description = "EC2 instance id of the VM-Series firewall."
  value       = module.vmseries.instance.id
}

output "bootstrap_bucket" {
  description = "S3 bootstrap bucket the firewall reads on first boot."
  value       = aws_s3_bucket.bootstrap.id
}

output "next_steps" {
  description = "What to check after apply."
  value       = <<-EOT
    1. Wait ~10-15 min for first boot + bootstrap + SCM registration.
    2. In SCM, confirm the device appears in folder "${var.scm_folder}" (Device onboarding).
    3. Once bound, retry the folder push (finding #12) — it should now have a target.
    4. Tear down when done:  terraform destroy
  EOT
}
