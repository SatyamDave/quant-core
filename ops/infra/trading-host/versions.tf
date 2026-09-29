terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.66"
    }
  }
}

# Credentials come from the operator's environment (SSO session or CI OIDC
# role), never from this repository.
provider "aws" {
  region = var.region
}
