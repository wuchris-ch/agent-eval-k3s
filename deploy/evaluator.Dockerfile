# syntax=docker/dockerfile:1.7
FROM node:22-bookworm-slim@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5 AS reviewer-build

WORKDIR /reviewer
COPY --from=reviewer package.json package-lock.json ./
RUN npm ci
COPY --from=reviewer tsconfig.json ./
COPY --from=reviewer src ./src
RUN npm run build && npm prune --omit=dev

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58 AS runtime

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update \
    && apt-get install --yes --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
COPY --from=reviewer-build /usr/local/bin/node /usr/local/bin/node
COPY --from=reviewer-build /reviewer /opt/reviewer
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
COPY benchmarks ./benchmarks
RUN uv sync --frozen --no-dev --extra observability
CMD ["agent-eval", "--help"]
