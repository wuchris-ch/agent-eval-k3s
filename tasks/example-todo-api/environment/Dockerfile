FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir fastapi uvicorn httpx pytest pytest-cov

# Coding-agent CLIs available for the agent phase. Convention: task images
# include the agents you want to evaluate; adapters expect them on PATH.
RUN curl -fsSL https://claude.ai/install.sh | bash
ENV PATH="/root/.local/bin:${PATH}"

RUN ARCH=$(uname -m) && case "$ARCH" in aarch64|arm64) T=aarch64 ;; *) T=x86_64 ;; esac \
    && curl -fsSL -o /tmp/codex.tar.gz \
       "https://github.com/openai/codex/releases/latest/download/codex-${T}-unknown-linux-musl.tar.gz" \
    && tar -xzf /tmp/codex.tar.gz -C /tmp \
    && mv "/tmp/codex-${T}-unknown-linux-musl" /usr/local/bin/codex \
    && chmod +x /usr/local/bin/codex \
    && rm /tmp/codex.tar.gz

COPY workspace /workspace
WORKDIR /workspace
