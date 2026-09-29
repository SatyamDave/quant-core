# ops/live -- the live trading host (#63)

> Status: `qc-bridge` (`engine/crates/bridge/`) and the agent service (`agent/`) run in
> fake/replay mode and against the synthetic mock broker out of the box. No real broker adapter
> ships with this repository (write your own: `agent/src/broker/README.md`) and no host is
> provisioned. This is the runbook and the tooling for when you provision one. The supervisor and the alert checker
> (`../../scripts/ops/alerts.py`, #64) are real and tested today against fixture child processes
> and fixture logs -- the fixtures stand in for a *live* run, which doesn't exist yet; the actual
> `qc-bridge`/`agent` binaries are exercised by `just agent-sim`, not by this ops tooling's tests.

`just live-canary SYMBOL` (`scripts/ops/live_canary.py`) runs preflight, the recorder, this
supervisor and the daily close as one command; see
[docs/runbooks/first-trading-day.md](../../docs/runbooks/first-trading-day.md).

## Host

**Recommendation: one small always-on Linux VM (or a dedicated Mac mini) per environment**
(`paper`, `canary`, `prod`), never a personal laptop and never the research/training host.
`ops/infra/trading-host/` already has the reviewed (not yet applied) shape for this: no public IP,
inbound SSH only from the VPN/bastion, IMDSv2, encrypted root disk, and an instance role scoped to
`quant-core/<environment>/*` secrets only (`ops/infra/README.md`). Provision from that module when
it is applied; until then, a manually-provisioned host must still follow those same rules by hand.

**Why a dedicated machine, not a laptop or the research host:**

- Root rule 9 and `ops/deploy/README.md`'s "Machines" section: research hosts, notebooks and
  agents must hold no venue credentials and must not be able to reach a trading host. A laptop
  that also runs research code (or anything else) is a research host that also trades -- exactly
  the shared-blast-radius setup those rules exist to prevent.
- A laptop sleeps, closes its lid, changes networks, and gets rebooted for OS updates on its
  owner's schedule, not the trading day's. Root rule 11 (kill switch within one second) and issue
  #63's "a crash halts trading until both are healthy again" both assume a process that is either
  running and supervised, or cleanly down -- not "the laptop was asleep for an unknown interval."
- Isolation limits blast radius: unrelated software on a shared machine (a research script, a
  browser extension, an editor plugin) is one more thing that can crash the process, read the
  environment a secret was loaded into, or open a port. A dedicated host's job is to run exactly
  two processes.

## Install

1. Provision the host (see above). Create a non-root service user (e.g. `quant-live`) that owns
   only its working directory, `ops/live/logs/` and `ops/live/state/` -- not the whole checkout.
2. Build the `qc-bridge` release binary (`just bridge`) and the agent service (`just agent-setup`;
   `agent/` is a plain npm package, no build step beyond installing pinned dependencies -- see
   `agent/CLAUDE.md`) and get both onto the host.
3. Copy this repo's `ops/live/supervisor.py`, `ops/live/bin/load-secrets.sh` and
   `ops/live/systemd/qc-live.service` to the host.
4. Configure secrets (below) and log shipping (below).
5. Install the systemd unit:
   ```
   sudo cp ops/live/systemd/qc-live.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now qc-live.service
   ```
   The unit runs `load-secrets.sh` to populate the environment, then execs `supervisor.py`, which
   starts and restarts the agent service (which spawns its own `qc-bridge` child via
   `QC_BRIDGE_BIN`/`QC_BRIDGE_ARGS` -- see `supervisor.py`'s module docstring for why
   `--bridge-cmd` is optional); systemd's own `Restart=on-failure` is a second layer in case the
   supervisor process itself dies.
6. **Prove fail-closed before trusting it**: stop one required secret (unset it, or point
   `--require-env` at a name that isn't set) and restart the unit; confirm it refuses to start and
   `ops/live/state/HALT` exists. This is the manual half of issue #63's "tested, not assumed" --
   the automated half is `tests/ops/test_supervisor.py`.
7. Clear a halt only after a human has read `ops/live/state/HALT`'s reason (`rm` it); nothing here
   clears it automatically, matching `qc_risk::KillSwitch`'s and `autonomy/PAUSE`'s "no reset."

## Secrets: environment or secret manager only, never in this repo

`ANTHROPIC_API_KEY` / `OPENROUTER_API_KEY` are read from the process environment only. Your
broker adapter (`QC_BROKER_MODULE`) loads its own broker credentials from your secret manager and
never hands them to the agent (`agent/src/broker/README.md`).
`ops/live/bin/load-secrets.sh` is the one hook that populates the environment, selected by
`QC_SECRETS_PROVIDER`:

- **`env` (default, implemented):** does nothing -- the values are already in the environment
  (e.g. via the systemd unit's `EnvironmentFile=`, itself populated by the host's own tooling, not
  by this repo). This is the only provider implemented here.
- **`1password` (documented, not implemented here):** `op run --env-file=ops/live/live.env.example
  -- <command>`, or `op read "op://<vault>/<item>/credential"` inside the hook to populate specific
  variables. Needs the 1Password CLI (`op`) signed in with a service account scoped to one vault.
- **`aws-secrets-manager` (documented, not implemented here):** `aws secretsmanager
  get-secret-value --secret-id quant-core/live/<environment> --query SecretString --output text`,
  parsed and exported into the process environment (never written to a persistent file -- if it
  must touch disk at all, a tmpfs-backed path only). Needs the host's instance role scoped to that
  one secret path, per `ops/infra/trading-host/`.
- **`keychain` (documented, not implemented here; only applicable if the host is a Mac):
  `security find-generic-password -a quant-live -s <item> -w`.

Each unimplemented provider's function in `load-secrets.sh` prints which one it is and exits
non-zero rather than pretending to succeed -- fail closed, not a silent no-op.

## Off-host log shipping (documented, pluggable)

`supervisor.py` redirects each child's stdout/stderr to `ops/live/logs/<name>.log` and writes its
own lifecycle events to `ops/live/logs/supervisor.log`, all JSON lines. In today's topology
(`--bridge-cmd` omitted) there is only one child, `ops/live/logs/agent.log`, and it already
contains qc-bridge's own stderr (`BridgeClient` pipes the bridge child's stderr into the agent
process's own stderr). Per `ops/deploy/README.md`'s audit-log requirement ("shipped off-host to an
append-only log bucket"), these files should also leave the host.
`ops/live/log-shipping/vector.toml.example` is a template for [Vector](https://vector.dev) doing
that: tail `ops/live/logs/*.log`, forward to a configurable sink. No real destination is
configured -- fill in `${QC_LOG_SINK_*}` and point `vector` at the finished file. Any shipper that
can tail a directory of JSON-lines files and forward it (Vector, Fluent Bit, even a
`logger`-based syslog forward) satisfies this; the template is a starting point, not a
requirement to use Vector specifically.

## Alerting

`scripts/ops/alerts.py` (#64) is meant to run periodically (a systemd timer or cron, every 1-2
minutes) against the log file that actually carries qc-bridge's output -- `ops/live/logs/agent.log`
in today's one-process topology, or `ops/live/logs/qc-bridge.log` if `--bridge-cmd` is used -- and
the agent's spend ledger (`out/agent/ledger.jsonl`). See that script's module docstring for the
exact log/ledger contract (qc-bridge doesn't emit the structured events the checker looks for yet)
and `docs/runbooks/` for what each alert means and how to respond.

When qc-bridge runs with `--follow` on a growing recording, also pass that recording (the first
positional in `QC_BRIDGE_ARGS`) as `--recording <path>`, with `--instrument` naming the
instrument TOML: the `recording_stalled` alert then fires if the file stops growing for more than
`--stall-after-sec` (default 120 s) during that instrument's market hours
(`docs/runbooks/recording-stalled.md`). `just shadow` runs the same check on every heartbeat.
