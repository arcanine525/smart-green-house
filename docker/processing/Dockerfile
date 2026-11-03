FROM eclipse-temurin:17.0.17_10-jre-jammy@sha256:03c5b280ad53c041741552f231f5b65d97be39ca74fa4c7c1c9ace7f42cc3c9e AS java

FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c

ENV JAVA_HOME=/opt/java/openjdk \
    PATH=/opt/java/openjdk/bin:$PATH \
    PYSPARK_PYTHON=python \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    JDBC_JAR=/opt/jdbc/postgresql-42.7.14.jar

COPY --from=java /opt/java/openjdk /opt/java/openjdk
RUN apt-get update \
    && apt-get install -y --no-install-recommends libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
COPY requirements.lock ./requirements.lock
RUN python -m pip install --no-cache-dir pip==25.2 setuptools==80.9.0 wheel==0.45.1 \
    && python -m pip install --no-cache-dir --no-build-isolation -r requirements.lock \
    && python -m pip check

# Fetch and verify at BUILD time. Spark never resolves Maven/Ivy at runtime.
RUN python -c "import hashlib, pathlib, urllib.request; p = pathlib.Path('/opt/jdbc/postgresql-42.7.14.jar'); p.parent.mkdir(parents=True); data = urllib.request.urlopen('https://repo.maven.apache.org/maven2/org/postgresql/postgresql/42.7.14/postgresql-42.7.14.jar', timeout=60).read(); assert hashlib.sha256(data).hexdigest() == '73914527305a40cce504b0d3d90b23caf565136912d607ae8ae7c5895512332c', 'JDBC checksum mismatch'; p.write_bytes(data)"

RUN useradd --create-home --uid 10001 greenhouse
COPY --chown=greenhouse:greenhouse spike ./spike
USER greenhouse
CMD ["python", "-m", "pytest", "-q", "-s", "-p", "no:cacheprovider", "spike/tests"]
