FROM python:3.12-slim@sha256:64695412729fbe8cf054511723820c82bbe5a077d4a6b4070cd4a7225d3422ce

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 agent \
    && useradd --uid 10001 --gid 10001 --create-home agent

RUN pip install --no-cache-dir \
    fastapi==0.139.0 \
    uvicorn==0.49.0 \
    httpx==0.28.1 \
    pytest==9.1.1 \
    pytest-cov==7.1.0

# Coding-agent CLIs available for the agent phase. Convention: task images
# include the agents you want to evaluate; adapters expect them on PATH.
ARG CLAUDE_CODE_VERSION=2.1.208
RUN ARCH=$(uname -m) && case "$ARCH" in \
      aarch64|arm64) PLATFORM=arm64; SHA=81e5dd48377bfd3cb733820e4e23f2294c925cba1e52dbeada69f46929f0c4a6 ;; \
      *) PLATFORM=x64; SHA=125372839bc827ca24dd72382627b291fbca615408d732fe3291bc16723ce7f3 ;; \
    esac \
    && curl -fsSL -o /usr/local/bin/claude \
       "https://downloads.claude.ai/claude-code-releases/${CLAUDE_CODE_VERSION}/linux-${PLATFORM}/claude" \
    && echo "${SHA}  /usr/local/bin/claude" | sha256sum -c - \
    && chmod 0755 /usr/local/bin/claude

ARG CODEX_VERSION=0.144.4
RUN ARCH=$(uname -m) && case "$ARCH" in aarch64|arm64) T=aarch64 ;; *) T=x86_64 ;; esac \
    && case "$T" in \
      aarch64) SHA=4d07243ef4ae6786b8b321d7aea3f9be4e1d2c597ae5407e7c1b9873334082b2 ;; \
      *) SHA=37c985be9d89e8c4f43b3aa0594c1213eac212d30ae2b95221f08fec807515d1 ;; \
    esac \
    && curl -fsSL -o /tmp/codex.tar.gz \
       "https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/codex-${T}-unknown-linux-musl.tar.gz" \
    && echo "${SHA}  /tmp/codex.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/codex.tar.gz -C /tmp \
    && mv "/tmp/codex-${T}-unknown-linux-musl" /usr/local/bin/codex \
    && chmod +x /usr/local/bin/codex \
    && rm /tmp/codex.tar.gz

COPY --chown=10001:10001 workspace /workspace
WORKDIR /workspace
USER 10001:10001
