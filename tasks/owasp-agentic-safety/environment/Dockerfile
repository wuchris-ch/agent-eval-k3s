FROM python:3.12-slim@sha256:c3d81d25b3154142b0b42eb1e61300024426268edeb5b5a26dd7ddf64d9daf28

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 agent \
    && useradd --uid 10001 --gid 10001 --create-home agent

RUN pip install --no-cache-dir \
    fastapi==0.139.0 \
    uvicorn==0.49.0 \
    httpx==0.28.1 \
    pytest==9.1.1

# Direct agent binaries are versioned and verified independently for each
# supported CPU architecture. Apt packages and transitive Python dependencies
# remain subject to their upstream repositories.
ARG CLAUDE_CODE_VERSION=2.1.210
RUN ARCH=$(uname -m) && case "$ARCH" in \
      aarch64|arm64) PLATFORM=arm64; SHA=84feb193c1d91f3b5eba836ed47c0e4dee953195abba950917c3e101eff174e8 ;; \
      x86_64|amd64) PLATFORM=x64; SHA=e7d2ceb53ed4c2ced1fe7fc1c6331c98dc5f7b4c9b2722d9c5fa3dd5dff6f719 ;; \
      *) echo "unsupported task image architecture: $ARCH" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o /usr/local/bin/claude \
       "https://downloads.claude.ai/claude-code-releases/${CLAUDE_CODE_VERSION}/linux-${PLATFORM}/claude" \
    && echo "${SHA}  /usr/local/bin/claude" | sha256sum -c - \
    && chmod 0755 /usr/local/bin/claude

ARG CODEX_VERSION=0.144.4
RUN ARCH=$(uname -m) && case "$ARCH" in \
      aarch64|arm64) T=aarch64; SHA=4d07243ef4ae6786b8b321d7aea3f9be4e1d2c597ae5407e7c1b9873334082b2 ;; \
      x86_64|amd64) T=x86_64; SHA=37c985be9d89e8c4f43b3aa0594c1213eac212d30ae2b95221f08fec807515d1 ;; \
      *) echo "unsupported task image architecture: $ARCH" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o /tmp/codex.tar.gz \
       "https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/codex-${T}-unknown-linux-musl.tar.gz" \
    && echo "${SHA}  /tmp/codex.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/codex.tar.gz -C /tmp \
    && mv "/tmp/codex-${T}-unknown-linux-musl" /usr/local/bin/codex \
    && chmod +x /usr/local/bin/codex \
    && rm /tmp/codex.tar.gz

COPY --chown=10001:10001 workspace /workspace
COPY --chown=0:0 submission_server.py /opt/agent-eval/submission_server.py
WORKDIR /workspace
USER 10001:10001
