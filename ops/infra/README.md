# ops/infra

Terraform (OpenTofu-compatible) for all infrastructure. Nothing is created by hand. No provider credentials live here: `terraform plan` uses the operator's short-lived SSO session, and CI uses an OIDC role once one exists.

## trading-host/

Skeleton for one engine host per environment (`paper`, `canary`, `prod`). It is reviewed and scanned, never applied from this repo yet.

- No public IP. The host sits in a private subnet; its only inbound rule is SSH from the VPN/bastion security group. No other port is open.
- Egress is HTTPS to the venue API ranges in `venue_egress_cidrs` only. Traffic leaves through a NAT gateway whose Elastic IP is the address allowlisted on each venue key (`ops/deploy/README.md`).
- IMDSv2 only, encrypted root disk with a customer-managed KMS key, detailed monitoring on.
- The instance role can read only `quant-core/<environment>/*` secrets.
- Research and training machines are in a separate account/VPC and have no route to this one.

Not here yet: VPC, NAT, bastion/VPN, KMS key, log shipping and remote state. They land as separate modules with their own review.

## Checks

CI runs `checkov` (framework terraform) and `trivy config` over this directory on every PR; High and Critical findings fail. Locally:

```bash
cd ops/infra/trading-host
tofu fmt -check && tofu init -backend=false && tofu validate
uvx checkov -d . --framework terraform
```

A suppression (`# checkov:skip=<id>: <reason>`) needs a reason that says why the check does not apply, per `SECURITY.md`.
