# syntax=docker/dockerfile:1

# Builds the wheel. It needs git and .git (hatch-vcs reads the version from history); the final image has neither, nor
# anything else from the build context.
FROM python:3.11-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY . .
RUN uv build --wheel --out-dir /dist

# One image for both roles: the broker (`argus-iris serve`, :8120) and the per-host agent (`argus-iris agent`, :9110).
FROM python:3.11-slim
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/
COPY --from=builder /dist /tmp/dist
RUN wheel=$(ls /tmp/dist/*.whl) \
    && uv pip install --system --no-cache "${wheel}[server,agent,cli]" \
    && rm -rf /tmp/dist
# Inside the container only: compose decides where the port is published (HOST_IP, 127.0.0.1 by default).
ENV IRIS_HOST=0.0.0.0 IRIS_AGENT_HOST=0.0.0.0
EXPOSE 8120 9110
CMD ["argus-iris", "serve"]
