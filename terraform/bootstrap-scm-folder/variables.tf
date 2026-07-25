variable "folder_name" {
  description = "SCM configuration folder to create (the GitOps target folder)."
  type        = string
  default     = "prod-edge"
}

variable "parent_folder" {
  description = "Parent in the SCM folder hierarchy. The NGFW container shown as 'All Firewalls' in the UI is 'ngfw-shared' in the config API (verified via /config/setup/v1/folders — GitOps sits under it)."
  type        = string
  default     = "ngfw-shared"
}

variable "description" {
  description = "Human-readable folder description shown in SCM."
  type        = string
  default     = "GitOps-managed edge firewall folder (bootstrap-scm-folder)."
}
