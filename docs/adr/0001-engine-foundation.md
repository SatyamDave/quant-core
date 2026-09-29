# ADR-0001: Engine foundation

## Status

Proposed. Date proposed: 2026-09-27. Not yet approved by a human.

## Context

quant-core needs a deterministic event-driven engine for backtesting and live execution, a fill simulator that models queue position for market making, and a research environment for automated factor discovery. Writing all of this from scratch would take a small team a long time and would repeat work that maintained open-source projects already do. The bootstrap spec proposes a default to validate: NautilusTrader for the engine and live adapters, hftbacktest for market-making fill simulation, and qlib plus RD-Agent only inside the research sandbox. Dependencies are pinned, not vendored, and a fork needs a documented reason.

Because operators may one day run this software for others, the license of each project matters as much as its features. All facts below were checked on 2026-09-27.

| Project | Latest release | Release date | License (from `gh api repos/<o>/<r>/license`) | Last commit on default branch |
|---|---|---|---|---|
| nautechsystems/nautilus_trader | v1.231.0 | 2026-08-02 | LGPL-3.0 | 2026-09-27 (`develop`) |
| nkaz001/hftbacktest | rust-v0.9.4 (Rust crate 0.9.4, Python package 2.4.4) | 2025-12-10 | MIT | 2025-12-23 (`master`) |
| microsoft/qlib | v0.9.7 | 2025-08-15 | MIT | 2026-09-16 (`main`) |
| microsoft/RD-Agent | v1.0.0 | 2026-09-23 | MIT | 2026-09-23 (`main`) |

Sources: [nautilus_trader release v1.231.0](https://github.com/nautechsystems/nautilus_trader/releases/tag/v1.231.0), [nautilus_trader LICENSE](https://github.com/nautechsystems/nautilus_trader/blob/develop/LICENSE), [last nautilus_trader commit checked](https://github.com/nautechsystems/nautilus_trader/commit/f73a6acb2d9dcdbad2c801fb8067646908558792); [hftbacktest release rust-v0.9.4](https://github.com/nkaz001/hftbacktest/releases/tag/rust-v0.9.4), [hftbacktest LICENSE](https://github.com/nkaz001/hftbacktest/blob/master/LICENSE), [last hftbacktest commit](https://github.com/nkaz001/hftbacktest/commit/5f3ec40b2afb764e0fea112f941ed85523ef4e88), [hftbacktest on PyPI](https://pypi.org/project/hftbacktest/); [qlib release v0.9.7](https://github.com/microsoft/qlib/releases/tag/v0.9.7), [qlib LICENSE](https://github.com/microsoft/qlib/blob/main/LICENSE), [last qlib commit](https://github.com/microsoft/qlib/commit/be725493eb1a6bbb42bf11b37aa7669f59610ff1); [RD-Agent release v1.0.0](https://github.com/microsoft/RD-Agent/releases/tag/v1.0.0), [RD-Agent LICENSE](https://github.com/microsoft/RD-Agent/blob/main/LICENSE), [last RD-Agent commit](https://github.com/microsoft/RD-Agent/commit/484776c211e4fbbeef03e0ec00d6bbee7362a4f4).

### What each project offers

**NautilusTrader.** The README describes the engine as written in Rust with Python as the control plane, spanning research, deterministic simulation and live execution in one event-driven architecture, and says "Trading systems can also be written entirely in Rust for mission-critical workloads." It lists Binance, Bybit, Coinbase, Kraken, OKX, Deribit and Hyperliquid among its integrations with a "stable" badge, and states "We aim to follow a bi-weekly release schedule." Its data catalog stores data in Parquet with Arrow schemas and supports S3, GCS and Azure object storage. Source: [README](https://github.com/nautechsystems/nautilus_trader), [data concepts](https://nautilustrader.io/docs/latest/concepts/data/). The project pins Rust 1.98.1 in its `rust-toolchain.toml`, which is the current stable release ([rust-toolchain.toml](https://github.com/nautechsystems/nautilus_trader/blob/develop/rust-toolchain.toml), [Rust stable channel](https://static.rust-lang.org/dist/channel-rust-stable.toml)). The Python package requires Python `>=3.12,<3.15` ([PyPI](https://pypi.org/project/nautilus_trader/)).

**hftbacktest.** The README lists "Order fill simulation that takes into account the order queue position", feed and order latency models, and "Full order book reconstruction based on Level-2 Market-By-Price and Level-3 Market-By-Order feeds". Live deployment is limited: "currently for Binance Futures and Bybit. (Rust-only)". Source: [README](https://github.com/nkaz001/hftbacktest). The last release was on 2025-12-10 and the last commit on 2025-12-23, so the project has had no commits on its default branch for about nine months as of this ADR. That is a maintenance risk: it has a single primary maintainer and no recent activity.

**qlib.** The README describes an AI-oriented quantitative investment platform whose bundled data and examples are for China and US equities; it does not mention crypto. PyPI classifiers stop at Python 3.12, and the 0.9.7 wheels are built for CPython 3.8 to 3.12 ([PyPI](https://pypi.org/project/pyqlib/)). Source: [README](https://github.com/microsoft/qlib).

**RD-Agent.** The README describes LLM-driven factor and model proposal loops that integrate with qlib, calls LLM providers through LiteLLM (OpenAI, Azure OpenAI, DeepSeek and others), runs generated code in Docker containers, and states that it "is aimed to facilitate research and development process in the financial industry and not ready-to-use for any financial investment or advice." PyPI requires Python `>=3.10` and declares classifiers only for 3.10 and 3.11 ([PyPI](https://pypi.org/project/rdagent/)). Source: [README](https://github.com/microsoft/RD-Agent).

### License implications for a future fund

This section is an engineering summary, not legal advice. Have a lawyer review it before relying on it commercially.

- **MIT (hftbacktest, qlib, RD-Agent).** Permissive. The obligation is to keep the copyright and license notice in copies. No source-disclosure obligation.
- **LGPL-3.0 (NautilusTrader).** Weak copyleft. The Python package on PyPI is labeled "LGPL-3.0-or-later" ([PyPI](https://pypi.org/project/nautilus_trader/)), while the Rust crates on crates.io are labeled "LGPL-3.0-only" (for example [nautilus-model 0.64.0](https://crates.io/crates/nautilus-model)). The license text ([LICENSE](https://github.com/nautechsystems/nautilus_trader/blob/develop/LICENSE)) sets conditions for anyone who conveys a "Combined Work", meaning a work produced by combining or linking an application with the library. Section 4 requires, among other things, prominent notice that the library is used, a copy of the GPL and LGPL, and either (0) conveying the library's source plus the application in a form that lets the recipient "recombine or relink the Application with a modified version" of the library, or (1) using "a suitable shared library mechanism" that works with a modified, interface-compatible version. Modifications to NautilusTrader itself are covered by the LGPL.
- **What that means for us, as far as engineering can tell.** The obligations attach when a Combined Work is conveyed to someone else. Running the engine on our own machines for our own trading is not obviously conveying. It becomes a real question if the software is ever given to a third party: a fund administrator, a separately organized fund entity, an investor's due-diligence team receiving binaries, or a customer. Rust crates are linked statically by default, which does not satisfy option (1), so a conveyed Rust binary that links NautilusTrader crates would likely need option (0): shipping the relinkable object code or source. Our own strategy code would stay ours, but we would have to be able to hand over what the license requires. **REQUIRES LEGAL REVIEW** before the fund stage, and before any binary leaves the operators' control.
- **Contributor agreements.** NautilusTrader and qlib both require a CLA for contributions ([nautilus_trader README](https://github.com/nautechsystems/nautilus_trader), [qlib README](https://github.com/microsoft/qlib)). This only matters if we upstream patches.

## Decision

1. **Use NautilusTrader as the engine foundation and source of live venue adapters, pinned to an exact release (currently v1.231.0), consumed as a dependency and not vendored.** Our own crates under `engine/crates/` (core, orderbook, gateway, oms, risk, strategy-runtime, inference) wrap or extend NautilusTrader types behind our own interfaces, so that the risk crate stays ours and the LGPL boundary is clear. The Phase 1 skeleton does not add the dependency yet; the first engine-core PR does, and it must record the pinned version and the reason for each crate it pulls in.
2. **Use hftbacktest for market-making fill simulation only (queue-position and latency models in backtests), pinned to rust-v0.9.4 / Python 2.4.4.** Do not use its live-trading support. Because it has had no commits for about nine months, treat it as a replaceable component: our fill-model interface lives in `backtest/fill_models/`, and hftbacktest is one implementation behind it.
3. **Use qlib and RD-Agent only inside `research/sandbox/`**, in their own uv environment pinned to a Python version both support (3.11 today, given RD-Agent's declared classifiers and qlib's 3.8 to 3.12 wheels). The sandbox holds no venue credentials and has no venue network access. RD-Agent's LLM calls are allowed there because the sandbox is not part of the live path (root rule 1). Anything RD-Agent produces is a candidate factor that re-enters the normal lifecycle gates and counts toward the trial total.
4. **No forks.** A fork requires a new ADR that names the upstream issue or PR we are waiting on and the plan to return to upstream.
5. **Upgrades** are ordinary PRs that move the pin, run `just check` and `just replay`, and note the upstream changelog entries that affect us.

## Alternatives

- **Write the engine entirely in-house.** Full control and no LGPL question, but it repeats adapter, order-book and simulation work that NautilusTrader already maintains, and it delays the first paper-trading result. It stays the fallback if legal review rejects LGPL.
- **hftbacktest as the whole engine.** It has good queue-position simulation, but live support is limited to Binance Futures and Bybit and the project has been inactive for about nine months, so it is not a safe foundation for live execution.
- **Vendor NautilusTrader into the repo.** Rejected by the spec and by this ADR: it makes upgrades painful and makes LGPL compliance harder, because our changes and theirs mix in one tree.
- **Use qlib as the research backtester for crypto.** Its data handling and examples target equities, and its Python support stops at 3.12. It is better kept to the sandbox for factor discovery.

## Consequences

- The engine inherits NautilusTrader's release cadence. A bi-weekly upstream schedule means frequent pin moves; we choose when to move.
- The LGPL question is open until each adopter reviews it for their own use. If the answer is unfavorable, the wrap-behind-our-interfaces approach limits how much has to be replaced.
- hftbacktest is a known maintenance risk. If it breaks on a Rust or Python upgrade, we either pin older toolchains in the backtest environment or replace it behind the fill-model interface.
- The research sandbox needs a Python version (3.11) that differs from the main research environment (3.12+ per ADR-0004). That is two uv environments, which is acceptable because the sandbox is isolated anyway.

## Review date

2026-12-27, or before the first paper-trading run, whichever comes first. Also review if hftbacktest has no release by that date, or if legal review of LGPL-3.0 concludes.
