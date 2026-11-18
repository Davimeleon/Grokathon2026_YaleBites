# Build the Vite frontend, then run the FastAPI app (API + live Yale Connect sync).

FROM node:22-bookworm-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /usr/local/bin/uv
WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    YALE_BITES_VAR_DIR=/data \
    YALE_BITES_SCHEDULER=1 \
    YALE_BITES_ONLINE_GEOCODING=1

COPY pyproject.toml uv.lock README.md ./
COPY app ./app
COPY data ./data
RUN uv sync --frozen --no-dev \
    && mkdir -p /data

COPY --from=web /web/dist ./web/dist

EXPOSE 8000
CMD ["uv", "run", "app"]
