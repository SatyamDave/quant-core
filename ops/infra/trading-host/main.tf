# Single trading host: no public IP, SSH only from the VPN/bastion security
# group, HTTPS egress only to venue ranges. A skeleton to review, not applied.

locals {
  name = "qc-trading-${var.environment}"
  tags = {
    project     = "quant-core"
    environment = var.environment
    role        = "trading"
  }
}

resource "aws_security_group" "trading" {
  name        = local.name
  description = "Trading host: inbound only from VPN/bastion, egress only to venues"
  vpc_id      = var.vpc_id
  tags        = local.tags
}

resource "aws_vpc_security_group_ingress_rule" "ssh_from_bastion" {
  # checkov:skip=CKV_AWS_24: the source is the bastion security group, not a CIDR; checkov misreads a rule with no cidr_ipv4 as 0.0.0.0/0
  security_group_id            = aws_security_group.trading.id
  description                  = "SSH from the VPN/bastion only"
  referenced_security_group_id = var.bastion_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 22
  to_port                      = 22
}

resource "aws_vpc_security_group_egress_rule" "https_to_venues" {
  for_each          = toset(var.venue_egress_cidrs)
  security_group_id = aws_security_group.trading.id
  description       = "HTTPS and WebSocket to venue APIs"
  cidr_ipv4         = each.value
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_iam_role" "trading" {
  name = local.name
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = local.tags
}

# The host reads only its own environment's venue keys; see ops/deploy/README.md.
resource "aws_iam_role_policy" "read_own_secrets" {
  name = "${local.name}-secrets"
  role = aws_iam_role.trading.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = "arn:aws:secretsmanager:${var.region}:*:secret:quant-core/${var.environment}/*"
    }]
  })
}

resource "aws_iam_instance_profile" "trading" {
  name = local.name
  role = aws_iam_role.trading.name
}

resource "aws_instance" "trading" {
  ami                         = var.ami_id
  instance_type               = var.instance_type
  subnet_id                   = var.private_subnet_id
  vpc_security_group_ids      = [aws_security_group.trading.id]
  iam_instance_profile        = aws_iam_instance_profile.trading.name
  associate_public_ip_address = false
  ebs_optimized               = true
  monitoring                  = true

  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required"
  }

  root_block_device {
    encrypted  = true
    kms_key_id = var.kms_key_arn
  }

  tags = merge(local.tags, { Name = local.name })
}
