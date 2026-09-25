# The nightly sync job (ADR 0005). Built inside Azure by infra/deploy.sh (az acr build).
# deploy.sh copies the base image into the project's own registry first (Docker Hub rate-limits shared
# build machines), then builds FROM that copy.
ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

COPY pyproject.toml README.md alembic.ini ./
COPY src ./src
COPY migrations ./migrations
RUN pip install . && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app

# Never run as root.
USER 10001
ENTRYPOINT ["sales-orders"]
CMD ["nightly-sync"]
