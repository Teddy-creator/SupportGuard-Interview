ARG PYTHON_BASE_IMAGE=python:3.11-slim
ARG UV_IMAGE=ghcr.io/astral-sh/uv@sha256:4f5d923c9dcea037f57bda425dd209f3ec643da2f0b74227f68d09dab0b3bb36
ARG CODE_VERSION=development

FROM ${UV_IMAGE} AS uv_cli

FROM ${PYTHON_BASE_IMAGE} AS builder

WORKDIR /app
ENV HF_HOME=/app/.cache/huggingface \
    UV_PROJECT_ENVIRONMENT=/app/runtime-venv

# uv is pinned by image digest and remains in the builder stage only.
COPY --from=uv_cli /usr/local/bin/uv /usr/local/bin/uv

# The workspace member manifest is needed to validate the frozen workspace
# lock. Validation source is deliberately never copied into this build.
COPY pyproject.toml uv.lock ./
COPY validation/pyproject.toml ./validation/pyproject.toml

# Runtime dependencies and build dependencies live in separate environments.
# Only runtime-venv is copied into the final image.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --extra local-embeddings --no-install-workspace
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_PROJECT_ENVIRONMENT=/app/build-venv \
    uv sync --locked --only-group build --no-install-workspace

RUN --mount=type=cache,target=/root/.cache/huggingface \
    set -eux; \
    downloaded=0; \
    for attempt in 1 2 3 4 5; do \
        if HF_HOME=/root/.cache/huggingface /app/runtime-venv/bin/python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('intfloat/multilingual-e5-small', revision='614241f622f53c4eeff9890bdc4f31cfecc418b3')"; then \
            downloaded=1; \
            break; \
        fi; \
        sleep "$((attempt * 2))"; \
    done; \
    test "$downloaded" = 1; \
    mkdir -p /app/.cache; \
    cp -a /root/.cache/huggingface /app/.cache/huggingface

COPY README.md ./README.md
COPY backend/src ./backend/src
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_PROJECT_ENVIRONMENT=/app/build-venv \
    uv build --package supportguard --wheel --no-build-isolation \
      --python /app/build-venv/bin/python --out-dir /app/wheels && \
    set -eux; \
    wheel="$(find /app/wheels -maxdepth 1 -type f -name 'supportguard-*.whl')"; \
    test -n "$wheel"; \
    test "$(find /app/wheels -maxdepth 1 -type f -name 'supportguard-*.whl' | wc -l)" = 1; \
    sha256sum "$wheel" | cut -d ' ' -f 1 > /app/runtime-wheel.sha256; \
    uv pip install --python /app/runtime-venv/bin/python --no-deps --no-build "$wheel"

FROM ${PYTHON_BASE_IMAGE} AS runtime
ARG CODE_VERSION=development

WORKDIR /app
RUN groupadd --system supportguard && useradd --system --gid supportguard supportguard

COPY --from=builder /app/runtime-venv /app/runtime-venv
COPY --from=builder /app/.cache /app/.cache
COPY --from=builder /app/runtime-wheel.sha256 /app/runtime-wheel.sha256

# Runtime data and the independent Interview migration remain explicit image
# inputs. Historical migration bytes are recoverable through the Archive and
# never enter the production image. Application Python comes only from the wheel.
COPY backend/alembic_baseline ./backend/alembic_baseline
COPY alembic-interview.ini ./alembic-interview.ini
COPY knowledge ./knowledge

LABEL org.opencontainers.image.revision="${CODE_VERSION}" \
      io.supportguard.component="backend" \
      io.supportguard.package-boundary="runtime-wheel-only"

ENV PATH="/app/runtime-venv/bin:$PATH" \
    CODE_VERSION="${CODE_VERSION}" \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/app/.cache/huggingface \
    TRANSFORMERS_OFFLINE=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000
USER supportguard
CMD ["supportguard", "serve"]
