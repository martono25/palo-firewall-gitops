output "folder_name" {
  description = "The SCM folder name (use as scm_folder / dgname when provisioning)."
  value       = scm_folder.this.name
}

output "folder_id" {
  description = "The SCM folder UUID."
  value       = scm_folder.this.id
}
