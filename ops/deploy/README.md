# ops/deploy

Deployment definitions (protected zone). Deploys are human-triggered, with a canary before full rollout. This file is the runtime security policy that every deploy definition here must follow.

## Secrets

- All venue keys and service credentials live in the cloud secret manager, under `quant-core/<environment>/<strategy>/<venue>`. Nothing is baked into images, config files or environment files in git.
- A process fetches only its own path at startup using its instance role. Engine code receives credentials as injected values and never reads them from repo files.
- Secrets are never logged. Startup logs record the secret path and version id, not the value.

## Venue keys

- Trade-only: withdrawal and transfer permissions are disabled on every key, checked when the key is created and again at each rotation.
- IP-allowlisted to the trading environment's NAT Elastic IP (`ops/infra/README.md`). A key that the venue cannot allowlist is not used.
- Scoped per strategy and per environment: one key per (strategy, environment, venue). Paper and canary never share a key with prod.
- Rotation: every 90 days, on any team member's departure, and immediately on suspected exposure. Rotation creates the new key, deploys it, confirms fills on the canary, then revokes the old key. Each rotation is recorded in the audit log.

## Machines

- Research and trading run on separate machines, accounts and networks. Research hosts, notebooks and agents hold no venue credentials and cannot reach trading hosts.
- Trading hosts accept no inbound traffic except SSH from the VPN/bastion.
- No AI API calls from trading hosts or any process holding venue credentials.

## Audit logs

- Shell sessions, deploys, secret reads (secret-manager access logs) and cloud API calls are shipped off-host to an append-only log bucket in a separate account, with object lock. The trading host cannot delete or modify them.
- Alerts on: secret reads from an unexpected role, key creation outside a rotation, and SSH logins outside a change window. Each alert links to a runbook in `docs/runbooks/`.

## Deploy rules

- Human-triggered only; no deploy on merge.
- Canary (tiny capital) before scaled rollout, with the kill switch verified on the canary first.
- Rollback is the previous image plus the previous config, and is tested before a prod deploy.
