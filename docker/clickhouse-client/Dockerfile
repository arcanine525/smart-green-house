FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/workspace/src
WORKDIR /workspace
# Client uses CPython stdlib only; no pip/network at runtime.
COPY src ./src
COPY tests/clickhouse ./tests/clickhouse
RUN useradd --create-home --uid 10001 greenhouse
USER greenhouse
CMD ["python", "-m", "greenhouse.clickhouse.client", "ping"]
