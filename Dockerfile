# Hyperfocus Dockerfile
# Multi-stage build for smaller image

# ═══════════════════════════════════════════════════════════════════
# Stage 1: Builder
# ═══════════════════════════════════════════════════════════════════
FROM python:3.12-slim as builder

WORKDIR /build

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Create virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# ═══════════════════════════════════════════════════════════════════
# Stage 2: Runtime
# ═══════════════════════════════════════════════════════════════════
FROM python:3.12-slim as runtime

# System deps for artefact extraction (PDF) and Docker CLI
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    openssh-client \
    poppler-utils \
    ca-certificates \
    curl \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    libffi-dev \
    shared-mime-info \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo \"$VERSION_CODENAME\") stable" > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*


# Labels
LABEL maintainer="Julien <contact@uyuni.fr>"
LABEL description="Hyperfocus - AI-powered development environment"
LABEL version="1.0.0"

# Create non-root user
RUN groupadd --gid 1000 hyperfocus && \
    groupadd --gid 999 docker && \
    useradd --uid 1000 --gid hyperfocus --groups docker --shell /bin/bash --create-home hyperfocus

WORKDIR /app

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy application code
COPY --chown=hyperfocus:hyperfocus backend/ ./backend/
COPY --chown=hyperfocus:hyperfocus frontend/ ./frontend/
COPY --chown=hyperfocus:hyperfocus migrations/ ./migrations/
COPY --chown=hyperfocus:hyperfocus alembic.ini .
COPY --chown=hyperfocus:hyperfocus run.py .

# Create directories for data
RUN mkdir -p /data/artefacts /data/db && \
    chown -R hyperfocus:hyperfocus /data

# Environment
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV ENVIRONMENT=production
ENV DEBUG=false
ENV ARTEFACTS_ROOT=/data/artefacts
ENV DATABASE_URL=sqlite+aiosqlite:////data/db/hyperfocus.db

# Switch to non-root user
USER hyperfocus

# Expose port
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8080/health', timeout=5).raise_for_status()"

# Run
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8080"]
