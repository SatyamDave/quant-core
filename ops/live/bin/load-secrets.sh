#!/usr/bin/env bash
# Pluggable secret loading hook for the live host (#63). Source this from the systemd unit
# (or a shell) before starting ops/live/supervisor.py: `source ops/live/bin/load-secrets.sh`.
#
# QC_SECRETS_PROVIDER selects how ANTHROPIC_API_KEY (or OPENROUTER_API_KEY) and
# QC_BROKER_MODULE get into the environment; the broker adapter QC_BROKER_MODULE names loads its
# own broker credentials (agent/src/broker/README.md). Never edit this file to hardcode a secret
# value; it only decides *how* they get read, never *what* they are.
set -euo pipefail

provider="${QC_SECRETS_PROVIDER:-env}"

case "$provider" in
  env)
    # Default and only implemented provider: the values are already in the environment
    # (systemd EnvironmentFile=, or however the shell that sources this got them). Nothing to do
    # -- this is the "implement env-based with a clear hook" option from issue #63.
    ;;
  1password)
    echo "load-secrets.sh: QC_SECRETS_PROVIDER=1password is documented, not implemented in this build." >&2
    echo "See ops/live/README.md's Secrets section for the 'op read'/'op run' shape." >&2
    exit 1
    ;;
  aws-secrets-manager)
    echo "load-secrets.sh: QC_SECRETS_PROVIDER=aws-secrets-manager is documented, not implemented in this build." >&2
    echo "See ops/live/README.md's Secrets section for the 'aws secretsmanager get-secret-value' shape." >&2
    exit 1
    ;;
  keychain)
    echo "load-secrets.sh: QC_SECRETS_PROVIDER=keychain is documented, not implemented in this build." >&2
    echo "See ops/live/README.md's Secrets section for the 'security find-generic-password' shape." >&2
    exit 1
    ;;
  *)
    echo "load-secrets.sh: unknown QC_SECRETS_PROVIDER '$provider'" >&2
    exit 1
    ;;
esac
