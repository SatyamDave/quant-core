# Base: official Rust image (stable channel), Debian bookworm. Pinned by digest, not just a tag,
# so the environment doesn't silently drift. Looked up 2026-09-27 via the Docker Hub registry API
# (multi-arch index digest for the `1-bookworm` tag):
#   curl -s "https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/rust:pull" \
#     | jq -r .token \
#     | xargs -I{} curl -s -H "Authorization: Bearer {}" \
#         -H "Accept: application/vnd.oci.image.index.v1+json" \
#         https://registry-1.docker.io/v2/library/rust/manifests/1-bookworm -D - -o /dev/null
# `docker` itself is not available in this environment, so this Dockerfile has not been built or
# tested locally — only the base-image lookup and the tool install commands were verified.
FROM rust:1-bookworm@sha256:93ce27a88655056a51dbdd8f5f2d7ddc071c7b0070fb288a37b5a285fc83971e

ENV CARGO_HOME=/usr/local/cargo \
    RUSTUP_HOME=/usr/local/rustup \
    PATH=/usr/local/cargo/bin:/home/vscode/.local/bin:$PATH

# just: the repo's task runner (justfile at the repo root). https://github.com/casey/just
RUN cargo install just --locked

# cargo-deny / cargo-audit: consumed by `just audit` and CI (Phase 4 security pipeline).
RUN cargo install cargo-deny --locked \
    && cargo install cargo-audit --locked

# gitleaks: pinned release binary (v8.30.1, latest as of 2026-09-27 per
# `gh api repos/gitleaks/gitleaks/releases/latest`), not a moving tag. TARGETARCH is set
# automatically by BuildKit (amd64 on Intel/CI runners, arm64 on Apple Silicon Docker Desktop,
# which builds arm64 by default) — both linux_x64 and linux_arm64 assets were confirmed to exist
# for this tag via `gh api repos/gitleaks/gitleaks/releases/tags/v8.30.1`. The checksum is
# verified against the release's own `gitleaks_8.30.1_checksums.txt`, not just trusted from curl.
ARG TARGETARCH
RUN set -eu; \
    case "${TARGETARCH}" in \
        amd64) asset=gitleaks_8.30.1_linux_x64.tar.gz ;; \
        arm64) asset=gitleaks_8.30.1_linux_arm64.tar.gz ;; \
        *) echo "unsupported TARGETARCH for gitleaks: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    cd /tmp; \
    curl -LsSf -o "${asset}" "https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/${asset}"; \
    curl -LsSf -o checksums.txt "https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_checksums.txt"; \
    grep -F "  ${asset}" checksums.txt | sha256sum -c -; \
    tar -xzf "${asset}" -C /usr/local/bin gitleaks; \
    rm "${asset}" checksums.txt; \
    gitleaks version

# Work as an unprivileged user (uid 1000, the devcontainer convention, so bind-mounted files keep
# the host user's ownership on Linux). It owns the cargo and rustup homes, because rustup installs
# the toolchain named in engine/rust-toolchain.toml on first use and cargo writes its registry.
ARG USERNAME=vscode
RUN groupadd --gid 1000 "${USERNAME}" \
    && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash "${USERNAME}" \
    && chown -R "${USERNAME}:${USERNAME}" "${CARGO_HOME}" "${RUSTUP_HOME}"
USER ${USERNAME}

# uv (installed per user into ~/.local/bin) also manages the Python 3.12 install
# (research/.python-version), so no system Python package is needed.
# https://docs.astral.sh/uv/getting-started/installation/
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && uv python install 3.12

# pre-commit: run via uv so it doesn't need a separate system Python.
RUN uv tool install pre-commit
