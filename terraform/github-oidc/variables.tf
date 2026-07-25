variable "region" {
  type    = string
  default = "ap-southeast-1"
}

variable "repo" {
  description = "GitHub owner/repo allowed to assume the CI role."
  type        = string
  default     = "martono25/palo-firewall-gitops"
}
