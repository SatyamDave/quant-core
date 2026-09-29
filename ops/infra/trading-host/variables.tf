variable "region" {
  description = "Region co-located with the venue's matching engine."
  type        = string
}

variable "environment" {
  description = "paper, canary or prod. Each environment gets its own host and keys."
  type        = string

  validation {
    condition     = contains(["paper", "canary", "prod"], var.environment)
    error_message = "environment must be paper, canary or prod."
  }
}

variable "vpc_id" {
  description = "VPC dedicated to trading. Research machines live elsewhere."
  type        = string
}

variable "private_subnet_id" {
  description = "Private subnet with no route from an internet gateway to the host."
  type        = string
}

variable "ami_id" {
  description = "Hardened, pinned image built by the image pipeline."
  type        = string
}

variable "instance_type" {
  description = "Instance size for the engine host."
  type        = string
  default     = "c7i.large"
}

variable "bastion_security_group_id" {
  description = "Security group of the VPN/bastion host, the only source allowed inbound."
  type        = string
}

variable "venue_egress_cidrs" {
  description = "Venue API ranges the host may reach over HTTPS."
  type        = list(string)
}

variable "kms_key_arn" {
  description = "Customer-managed KMS key for disk and log encryption."
  type        = string
}
