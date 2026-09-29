# ADR-0002: Dependencies and licenses

## Status

Proposed. Date proposed: 2026-09-27. Not yet approved by a human.

## Context

quant-core is meant to grow into a fund. A fund's software may be inspected by administrators, auditors and investors, and some licenses impose obligations when software is conveyed to others. We need to know the license of every direct dependency before it lands, and to flag copyleft licenses for legal review before the fund stage.

No dependency is installed yet: `main` holds only `README.md` and `.gitignore`. This ADR records the licenses of the direct dependencies we can name now: the libraries chosen in ADR-0001 and the planned libraries from the bootstrap spec. Licenses of transitive dependencies are not covered here; Phase 4 enforces them automatically (see Decision).

Every entry below was checked on 2026-09-27. "Repo license" comes from `gh api repos/<owner>/<repo>/license`. "Registry license" is the metadata on PyPI or crates.io for the version named. Where the two differ, both are shown.

### Engine and research foundations (ADR-0001)

| Dependency | Ecosystem | Version checked | Repo license | Registry license | Copyleft? |
|---|---|---|---|---|---|
| nautilus_trader | PyPI | 1.231.0 | LGPL-3.0 ([LICENSE](https://github.com/nautechsystems/nautilus_trader/blob/develop/LICENSE)) | LGPL-3.0-or-later ([PyPI](https://pypi.org/project/nautilus_trader/)) | **Yes, weak copyleft** |
| nautilus-* crates (e.g. nautilus-model) | crates.io | 0.64.0 | LGPL-3.0 | LGPL-3.0-only ([crates.io](https://crates.io/crates/nautilus-model)) | **Yes, weak copyleft** |
| hftbacktest | crates.io / PyPI | 0.9.4 / 2.4.4 | MIT ([LICENSE](https://github.com/nkaz001/hftbacktest/blob/master/LICENSE)) | MIT ([crates.io](https://crates.io/crates/hftbacktest), [PyPI](https://pypi.org/project/hftbacktest/)) | No |
| pyqlib (qlib) | PyPI | 0.9.7 | MIT ([LICENSE](https://github.com/microsoft/qlib/blob/main/LICENSE)) | MIT ([PyPI](https://pypi.org/project/pyqlib/)) | No |
| rdagent (RD-Agent) | PyPI | 1.0.0 | MIT ([LICENSE](https://github.com/microsoft/RD-Agent/blob/main/LICENSE)) | MIT ([PyPI](https://pypi.org/project/rdagent/)) | No |

### Planned Python dependencies

| Dependency | Version checked | Repo license | Registry license | Copyleft? |
|---|---|---|---|---|
| polars | 1.44.2 | MIT ([LICENSE](https://github.com/pola-rs/polars/blob/main/LICENSE)) | MIT ([PyPI](https://pypi.org/project/polars/)) | No |
| duckdb | 1.5.5 | MIT ([LICENSE](https://github.com/duckdb/duckdb/blob/v2.0-cyanoptera/LICENSE)) | MIT ([PyPI](https://pypi.org/project/duckdb/)) | No |
| pyarrow | 25.0.1 | Apache-2.0 ([LICENSE.txt](https://github.com/apache/arrow/blob/main/LICENSE.txt)) | Apache-2.0 ([PyPI](https://pypi.org/project/pyarrow/)) | No |
| mlflow | 3.16.1 | Apache-2.0 ([LICENSE.txt](https://github.com/mlflow/mlflow/blob/master/LICENSE.txt)) | Apache-2.0 ([PyPI](https://pypi.org/project/mlflow/)) | No |
| lightgbm | 4.7.0 | MIT ([LICENSE](https://github.com/lightgbm-org/LightGBM/blob/main/LICENSE)) | No license field in PyPI metadata ([PyPI](https://pypi.org/project/lightgbm/)); rely on the repo license | No |
| onnx | 1.23.0 | Apache-2.0 ([LICENSE](https://github.com/onnx/onnx/blob/main/LICENSE)) | Apache-2.0 ([PyPI](https://pypi.org/project/onnx/)) | No |
| onnxruntime | 1.30.0 | MIT ([LICENSE](https://github.com/microsoft/onnxruntime/blob/main/LICENSE)) | MIT ([PyPI](https://pypi.org/project/onnxruntime/)) | No |

The `microsoft/LightGBM` repository now redirects to `lightgbm-org/LightGBM`; the license did not change.

### Planned Rust dependencies

| Crate | Version checked | Repo license | Registry license | Copyleft? |
|---|---|---|---|---|
| criterion | 0.8.2 | Apache-2.0 and MIT files ([repo](https://github.com/bheisler/criterion.rs)) | Apache-2.0 OR MIT ([crates.io](https://crates.io/crates/criterion)) | No |
| proptest | 1.11.0 | Apache-2.0 and MIT files ([repo](https://github.com/proptest-rs/proptest)) | MIT OR Apache-2.0 ([crates.io](https://crates.io/crates/proptest)) | No |
| serde | 1.0.229 | Apache-2.0 and MIT files ([repo](https://github.com/serde-rs/serde)) | MIT OR Apache-2.0 ([crates.io](https://crates.io/crates/serde)) | No |
| tokio | 1.53.1 | MIT ([LICENSE](https://github.com/tokio-rs/tokio/blob/master/LICENSE)) | MIT ([crates.io](https://crates.io/crates/tokio)) | No |

`gh api .../license` reports a single SPDX id per repository, so for dual-licensed crates it shows only one of the two license files. The crates.io expression is the authoritative one for what we link.

### Developer tooling (not linked into our code)

| Tool | Version checked | License |
|---|---|---|
| Rust toolchain | 1.98.1 ([release](https://github.com/rust-lang/rust/releases/tag/1.98.1)) | MIT and Apache-2.0 (LICENSE-MIT, LICENSE-APACHE and COPYRIGHT in the [repo](https://github.com/rust-lang/rust)) |
| uv | 0.12.19 ([release](https://github.com/astral-sh/uv/releases/tag/0.12.19)) | MIT OR Apache-2.0 (LICENSE-MIT and LICENSE-APACHE in the [repo](https://github.com/astral-sh/uv)) |
| just | 1.58.0 ([release](https://github.com/casey/just/releases/tag/1.58.0)) | CC0-1.0 |
| pre-commit | 4.6.2 ([release](https://github.com/pre-commit/pre-commit/releases/tag/v4.6.2)) | MIT |
| ruff | 0.16.9 ([release](https://github.com/astral-sh/ruff/releases/tag/0.16.9)) | MIT |

Tools that only run on developer machines or in CI and are not distributed with our software do not create conveyance obligations for our code.

## Decision

1. **Allowed without review:** MIT, Apache-2.0, BSD-2-Clause, BSD-3-Clause, ISC, Zlib, Unicode-3.0, CC0-1.0, and dual or multi licenses that include one of these.
2. **Allowed with a flag for legal review before the fund stage:** LGPL-2.1 and LGPL-3.0 (any variant), MPL-2.0. NautilusTrader is the only such direct dependency today. It is allowed now because nothing is conveyed during the research and paper stages, and it is flagged **REQUIRES LEGAL REVIEW** (see ADR-0001, "License implications").
3. **Not allowed without a new ADR and legal sign-off:** GPL (any version), AGPL (any version), SSPL, BUSL and other source-available licenses, and any dependency with no license.
4. **Enforcement belongs to Phase 4.** `engine/deny.toml` encodes rules 1 to 3 for Rust through `cargo deny check licenses`, with the LGPL crates listed as explicit, commented exceptions rather than a blanket allow. Python gets an equivalent license check over `uv.lock` in CI. A new direct dependency PR updates the tables in this ADR (or a successor) in the same PR.
5. **Pin, do not vendor.** Every dependency is pinned by lockfile (`Cargo.lock`, `uv.lock`). Vendoring a large project requires an ADR.

## Alternatives

- **Permissive-only policy (no LGPL).** Simplest legally, but it rules out NautilusTrader and would force an in-house engine (see ADR-0001 alternatives).
- **No policy until the fund stage.** Cheaper now, but copyleft code that spreads through the tree for a year is expensive to remove later.
- **Manual review of each dependency with no tooling.** Does not scale to transitive dependencies, of which a Rust and Python stack will have hundreds.

## Consequences

- Every PR that adds a dependency carries a small documentation cost.
- Transitive dependencies may bring licenses not listed here. The Phase 4 license checks are expected to surface some, and each one gets a decision, not a silent allow.
- The LGPL question stays open and visible; each adopter reviews it for their own use.

## Review date

When Phase 4 lands the automated license checks (to reconcile this list with their first report), and again before any software is conveyed outside the operators or the fund stage begins.
