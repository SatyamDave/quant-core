# ADR-0004: Languages and toolchain

## Status

Proposed. Date proposed: 2026-09-27. Not yet approved by a human.

## Context

The live path must be deterministic, fast and free of AI calls (root rules 1 and 7). Research and model training need a large numerical ecosystem. Contributors, human and agent, need one command to set up and one command to check everything. The bootstrap spec proposes Rust stable for the engine, Python 3.12+ managed by uv for research and ML, `just` as the command runner, pre-commit for local gates, and Docker plus a devcontainer for a reproducible environment.

Facts checked on 2026-09-27:

- The current Rust stable release is 1.98.1, dated 2026-09-03 on GitHub ([release](https://github.com/rust-lang/rust/releases/tag/1.98.1); [stable channel manifest](https://static.rust-lang.org/dist/channel-rust-stable.toml)). NautilusTrader pins the same version in its [`rust-toolchain.toml`](https://github.com/nautechsystems/nautilus_trader/blob/develop/rust-toolchain.toml).
- Python 3.14 is the newest release line (3.14.7); 3.12 receives security fixes only and reaches end of life on 2028-10-31; 3.13 active support ends 2026-10-01 ([endoflife.date/python](https://endoflife.date/python)).
- Python constraints of the ADR-0001 libraries: nautilus_trader requires `>=3.12,<3.15` ([PyPI](https://pypi.org/project/nautilus_trader/)); hftbacktest requires `>=3.11` with classifiers for 3.11 to 3.13 ([PyPI](https://pypi.org/project/hftbacktest/)); pyqlib 0.9.7 ships wheels for CPython 3.8 to 3.12 only ([PyPI](https://pypi.org/project/pyqlib/)); rdagent requires `>=3.10` with classifiers for 3.10 and 3.11 only ([PyPI](https://pypi.org/project/rdagent/)).
- Current tool releases: uv 0.12.19 ([release](https://github.com/astral-sh/uv/releases/tag/0.12.19)), just 1.58.0 ([release](https://github.com/casey/just/releases/tag/1.58.0)), pre-commit 4.6.2 ([release](https://github.com/pre-commit/pre-commit/releases/tag/v4.6.2)), ruff 0.16.9 ([release](https://github.com/astral-sh/ruff/releases/tag/0.16.9)).

## Decision

1. **Rust stable for `engine/`, pinned exactly in `engine/rust-toolchain.toml`** (1.98.1 today, matching NautilusTrader so the two build with the same compiler). Moving the pin is its own PR. Nightly is allowed only for tools that need it (Miri, cargo-fuzz) and never for code that ships.
2. **Python 3.12 as the pinned interpreter for `research/` and `ml/`**, managed by uv with a committed `uv.lock`. 3.12 is the newest version that every main-environment dependency supports today (qlib's wheels stop at 3.12). Moving to 3.13 or 3.14 is a later PR once the lockfile resolves on it.
3. **A separate uv project for `research/sandbox/` pinned to Python 3.11**, because RD-Agent declares support only for 3.10 and 3.11. The sandbox stays isolated (ADR-0001), so a different interpreter there costs little.
4. **`just` is the only documented entry point.** Every command in CLAUDE.md files and CI calls a `just` recipe (`just setup`, `just check`, `just test`, `just bench`, `just replay`, and so on), so local runs and CI run the same thing.
5. **pre-commit runs the fast local gates** (formatters, lint, secret scan, large-file and private-key checks). CI runs the same hooks plus the slow checks; pre-commit is a convenience, CI is the gate.
6. **Docker plus a devcontainer** give a one-command environment that installs the pinned Rust toolchain, uv, just and pre-commit. The devcontainer is the reference environment for the acceptance check "`just setup && just check` passes from a fresh clone".

## Alternatives

- **C++ for the engine.** Mature low-latency ecosystem, but no memory-safety guarantees, and NautilusTrader's core is Rust, so C++ would add a language instead of removing one.
- **Python for everything, with Numba or Cython in hot spots.** Faster to start, but the live path would depend on a garbage-collected runtime, and determinism and latency budgets would be harder to prove.
- **Poetry, pip-tools or conda instead of uv.** All workable; uv gives one tool for interpreter install, locking and running, which keeps the `just` recipes short.
- **make instead of just.** Available everywhere, but its tab rules and implicit rules are a common source of mistakes; just is purpose-built for command running.
- **One Python version everywhere.** Not possible today without dropping RD-Agent or qlib, given their declared version support.

## Consequences

- Two Python environments to keep resolvable (main at 3.12, sandbox at 3.11).
- Rust pin moves follow NautilusTrader's pin in practice; if we diverge, we must confirm NautilusTrader still builds with our pin.
- Contributors must install just and uv, or use the devcontainer.
- Python 3.12 is already in security-fix-only mode, so this pin should move within a year.

## Review date

2027-03-27, or when qlib and RD-Agent both declare support for a newer Python, whichever comes first.
