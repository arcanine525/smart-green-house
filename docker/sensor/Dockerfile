FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /workspace
COPY requirements-sensor.lock ./requirements-sensor.lock
RUN python -m pip install --no-cache-dir pip==25.2 setuptools==80.9.0 wheel==0.45.1 \
    && python -m pip install --no-cache-dir -r requirements-sensor.lock \
    && python -m pip check
COPY pyproject.toml ./pyproject.toml
COPY src ./src
RUN python -m pip install --no-cache-dir --no-deps --no-build-isolation . \
    && useradd --create-home --uid 10001 greenhouse \
    && mkdir -p /data/spool /data/outbox /data/ready \
    && chown -R greenhouse:greenhouse /data/spool /data/outbox /data/ready
USER greenhouse
CMD ["python", "-m", "greenhouse", "simulate"]
