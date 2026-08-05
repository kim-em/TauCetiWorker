# A disposable Linux host for the Tau Ceti worker. The agent runs in host mode inside this
# container; it does not use the more restrictive Bubble sandbox.
FROM node:22-bookworm

SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ARG TAUCETI_LEAN_TOOLCHAIN=leanprover/lean4:v4.32.0

# Runtime tools for tauceti and its agents, plus a native toolchain for Lean builds. Debian
# package revisions deliberately track Bookworm's security repository instead of being frozen.
# hadolint ignore=DL3008
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        git \
        jq \
        python3 \
        python3-requests \
        ripgrep \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Lean (elan/lake) and uv/uvx. Both installers select binaries for the build architecture.
RUN curl -fsSL https://elan.lean-lang.org/elan-init.sh | sh -s -- -y \
    && /root/.elan/bin/elan toolchain install "$TAUCETI_LEAN_TOOLCHAIN" \
    && /root/.elan/bin/elan default "$TAUCETI_LEAN_TOOLCHAIN" \
    && curl -LsSf https://astral.sh/uv/install.sh | sh

# Subscription authentication is performed at runtime and persisted by compose.yaml. Pin the
# clients so rebuilding a deployment uses known client versions; the scheduled image build detects
# when a pinned client or its service contract stops working. Keep this after Lean so a client-version
# bump does not invalidate the much larger toolchain layer.
ARG CLAUDE_CODE_VERSION=2.1.220
ARG CODEX_VERSION=0.145.0
RUN npm install -g \
    "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    "@openai/codex@${CODEX_VERSION}"

ENV PATH="/root/.elan/bin:/root/.local/bin:${PATH}" \
    ELAN_HOME=/root/.elan \
    UV_CACHE_DIR=/root/.cache/uv \
    DISABLE_AUTOUPDATER=1 \
    IS_SANDBOX=1 \
    PYTHONUNBUFFERED=1

# Keep the toolchain layers reusable while making every image contain the exact checked-out
# worker revision under test (including pull-request changes).
WORKDIR /opt/tauceti
COPY tauceti pyproject.toml ./
COPY prompts ./prompts
COPY scripts ./scripts
COPY tauceti_worker ./tauceti_worker

RUN install -m 0755 scripts/curl-no-progress /usr/local/bin/curl \
    && install -m 0755 scripts/oauth_refresh_loop.py /usr/local/bin/tauceti-oauth-refresh \
    && install -m 0755 scripts/docker-entrypoint /usr/local/bin/tauceti-entrypoint \
    && chmod 0755 tauceti scripts/claim.sh scripts/gh-safe-pr-create scripts/git-safe-push \
    && ./tauceti --help >/dev/null \
    && git config --system user.name "TauCeti Worker" \
    && git config --system user.email "tauceti-worker@users.noreply.github.com" \
    && git config --system credential.https://github.com.helper "" \
    && git config --system --add credential.https://github.com.helper "!gh auth git-credential"

ENTRYPOINT ["/usr/local/bin/tauceti-entrypoint"]
CMD ["./tauceti", "work", "--loop"]
