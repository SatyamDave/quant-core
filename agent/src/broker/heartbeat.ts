// Issue #44 reopened ("Stop everything even if the agent itself is stuck"): a hung decision loop
// (a decider whose call never returns, or a feed that goes quiet) never calls the bridge again --
// so even though qc-bridge itself now polls --kill-file independently of stdin
// (engine/crates/bridge/src/main.rs's watcher thread), nothing in THIS process would ever ask
// `status` again to find out, and nothing would ever finish an external-mode halt's cancels at
// the real broker (protocol v1.2.1's `pending_cancels`/`drain_halt_cancels`,
// engine/crates/bridge/src/engine.rs's `pending_cancels`).
//
// This is that "something": a real wall-clock `setInterval`, deliberately decoupled from
// runLoop's own await chain (agent/src/loop.ts calls the bridge only between one decision and the
// next -- exactly the call this class must not depend on), that keeps polling `status` on its own
// cadence regardless of what the decision loop is doing. Once it observes `halted`, it pulls
// every pending halt-cancel approval and finishes the cancel at the broker
// (BrokerGateway.cancelOrder, which itself verifies the approval before ever calling the broker),
// retrying every tick -- the approval is freshly minted server-side each time, so it never goes
// stale -- until nothing is left open there.
import type { BridgeClient } from "../bridge.js";
import type { PendingCancel } from "../types.js";
import type { BrokerGateway } from "./gateway.js";
import type { AssetClass } from "./types.js";

export interface HeartbeatOptions {
  bridge: BridgeClient;
  gateway: BrokerGateway;
  /** This build supports exactly one instrument per bridge process (wave2-spec.md's gap list);
   *  `status`/`pending_cancels` carry only `client_order_id`, not the instrument, so this is
   *  threaded in from the same place cli.ts already gets it (QC_INSTRUMENT). */
  instrument: string;
  assetClass: AssetClass;
  /** Default 1s: CLAUDE.md rule 11's one-second kill-switch budget, and wave2-spec.md's own
   *  "calls status every <=1s" requirement for this exact heartbeat. */
  intervalMs?: number;
  /** Fires once, the first time a tick observes `halted` (never again for the same halt --
   *  there is no un-halting within a process, root rule: a human restarts it). Logging/alerting
   *  hook; must not throw (this class does not await it and does not itself handle a throw from
   *  it, same contract as GatewayConfig.onAmbiguousOutcome elsewhere in this directory). */
  onHalted?: (reason: string) => void;
}

const DEFAULT_INTERVAL_MS = 1_000;

export class Heartbeat {
  private readonly intervalMs: number;
  private timer: ReturnType<typeof setInterval> | undefined;
  private ticking = false;
  private halted = false;

  constructor(private readonly opts: HeartbeatOptions) {
    this.intervalMs = opts.intervalMs ?? DEFAULT_INTERVAL_MS;
  }

  /** Idempotent: a second call while already running is a no-op rather than a second timer. */
  start(): void {
    if (this.timer) return;
    this.timer = setInterval(() => {
      void this.tick();
    }, this.intervalMs);
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = undefined;
  }

  /** Whether this heartbeat has ever observed the bridge halted. Test/observability hook. */
  get isHalted(): boolean {
    return this.halted;
  }

  private async tick(): Promise<void> {
    // setInterval keeps firing on the wall clock regardless of how long the previous tick's
    // bridge round trip (or broker calls) took; piling a second tick on top of a slow one would
    // only make things worse (duplicate cancelOrder calls racing each other), not faster.
    if (this.ticking) return;
    this.ticking = true;
    try {
      const resp = await this.opts.bridge.status();
      if (!resp.ok || !resp.status) return;
      const status = resp.status;
      if (status.halted) {
        if (!this.halted) {
          this.halted = true;
          this.opts.onHalted?.(status.halted);
        }
        await this.drainCancels(status.pending_cancels ?? []);
      }
    } catch (err) {
      // A failed heartbeat tick (the bridge process died, or this one round trip timed out) is
      // not silently swallowed, but it is also not this class's job to escalate -- the bridge
      // dying is already handled where it matters (BridgeClient.failEverything rejects every
      // other in-flight call too, and agent/tests/cli-exit.test.ts proves the whole agent
      // process then exits non-zero).
      console.error("heartbeat: status check failed:", err);
    } finally {
      this.ticking = false;
    }
  }

  private async drainCancels(pending: PendingCancel[]): Promise<void> {
    for (const p of pending) {
      try {
        await this.opts.gateway.cancelOrder(p.client_order_id, this.opts.instrument, this.opts.assetClass, p.approval);
      } catch (err) {
        // Same reasoning as BrokerGateway's own report() failure path: the next tick retries
        // this exact order (drain_halt_cancels/status always re-lists anything still open), so a
        // log-and-continue here is correct -- throwing would kill the timer callback silently
        // (an unhandled rejection inside a fire-and-forget `void this.tick()`) and stop retrying
        // every other pending cancel in this same tick, too.
        console.error(`heartbeat: cancelOrder for client_order_id ${p.client_order_id} failed:`, err);
      }
    }
  }
}
