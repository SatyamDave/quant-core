# ui/ — the agent's console

A single-page web console for quant-core. It is plain HTML, CSS and JS: no build step, no
dependencies, no CDN. It talks only to the control API on the same origin (`/api/*`).

## Run

```sh
just control-api          # serves the control API and this folder at http://127.0.0.1:7070
open http://127.0.0.1:7070
```

The API binds to 127.0.0.1 only. Do not expose it on a public interface: anyone who can reach it
can submit order intents and engage the kill switch.

## What it shows

<!-- Screenshot placeholder: docs/assets/ui-console.png, a desktop view in dark mode. The header
     shows the PAPER badge and the red KILL SWITCH button. Below it is the "Today vs limits" card
     (daily notional meter at 62%, daily loss, order rate), the decision feed with BUY/HOLD rows and
     the agent's rationale, the manual order-intent form, and the Rules grid of tighten-only limit
     editors. -->

- **Header:** a mode badge and a kill switch. The badge reads SIM (simulated venue), PAPER (sim that
  follows live quotes) or LIVE (external venue, so orders reach your broker adapter). The kill switch
  asks for confirmation, then `POST /api/kill`. It is one-way: to release it, a human deletes the
  kill file and restarts the bridge.
- **Today vs limits:** today's usage against `max_daily_notional`, `max_daily_loss` and
  `max_order_rate_per_sec`, plus position, decision count, accepted orders and halt reason. A meter
  shows "not reported" when the API does not supply a usage number.
- **Decision feed:** `GET /api/decisions`, polled every 2s, newest first. Each row has the action, the
  risk outcome (accepted or rejected, with the reason) and the agent's rationale. "Orders only" filters
  it to accepted orders.
- **Manual order intent:** `POST /api/orders`. It goes through the same engine risk checks as any
  agent intent. The control API refuses external mode unless the operator enables it, so the default is
  sim/paper.
- **Rules:** `GET /api/rules`, with one editor per limit. Tightening a limit posts to `/api/rules`.
  Loosening is blocked in the form, with an explanation that it needs a human edit to
  `config/limits` with two approvals. The server rejects it too, so the UI check is only for
  convenience. A tightened limit takes effect when the bridge restarts.

If the API is unreachable, a banner says so, the badge shows OFFLINE, order submission is disabled,
and the page keeps retrying every 2s.

## Design notes

Light and dark follow `prefers-color-scheme`. The layout collapses to one column below 860px. All
controls have labels and visible focus rings. Text from the API, including agent rationale, is
rendered with `textContent` and never as HTML.
