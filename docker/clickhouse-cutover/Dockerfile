# Repackage the already locked/tested local image without dependency downloads.
ARG BASE=greenhouse-ch-walkthrough-processing:20261107-cold-fix
FROM ${BASE}
USER root
COPY pyproject.toml ./pyproject.toml
COPY src ./src
RUN python -m pip install --no-cache-dir --no-deps --no-build-isolation .
COPY --chown=greenhouse:greenhouse tests ./tests
USER greenhouse
