variable "folder" {
  description = "The greenfield SCM folder to create the throwaway objects in. LAB ONLY."
  type        = string
  # no default on purpose — you must name your test folder explicitly
}

variable "name_prefix" {
  description = "Prefix for the throwaway object names, so they're obvious and easy to find."
  type        = string
  default     = "spike-test"
}

variable "test_cidr" {
  description = "A harmless CIDR for the test address object."
  type        = string
  default     = "10.255.255.0/24"
}

variable "test_port" {
  description = "Port for the test service object (string — the provider takes a string)."
  type        = string
  default     = "443"
}

variable "from_zones" {
  description = "Source zones. Defaults to any so the test does not depend on zones existing in a greenfield folder."
  type        = list(string)
  default     = ["any"]
}

variable "to_zones" {
  description = "Destination zones. Defaults to any (see from_zones)."
  type        = list(string)
  default     = ["any"]
}
