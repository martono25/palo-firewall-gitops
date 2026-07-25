variable "folder_name" {
  description = "SCM configuration folder to create (the GitOps target folder)."
  type        = string
  default     = "prod-edge"
}

variable "parent_folder" {
  description = "Parent in the SCM folder hierarchy (NGFW root is 'All Firewalls')."
  type        = string
  default     = "All Firewalls"
}

variable "description" {
  description = "Human-readable folder description shown in SCM."
  type        = string
  default     = "GitOps-managed edge firewall folder (bootstrap-scm-folder)."
}
