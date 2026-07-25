variable "region" {
  type    = string
  default = "ap-southeast-1"
}

variable "repo" {
  description = "GitHub owner/repo allowed to assume the CI role."
  type        = string
  default     = "martono25/palo-firewall-gitops"
}

# GitHub's immutable numeric-ID subject form: repo:<owner>@<owner_id>/<repo>@<repo_id>
# Sent when the org/repo enables ID-based subject claims. Leave "" to disable.
variable "repo_id_subject" {
  description = "Immutable-ID subject prefix (repo:<owner>@<id>/<repo>@<id>), or empty."
  type        = string
  default     = "repo:martono25@287233980/palo-firewall-gitops@1309374040"
}
