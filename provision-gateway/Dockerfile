# provision-gateway Dockerfile
# Multi-stage: copy docker CLI from docker:cli image
#
# Build with proxy:
#   docker build --build-arg HTTP_PROXY=http://proxy:port --build-arg HTTPS_PROXY=http://proxy:port .

FROM docker:cli AS docker-cli

FROM python:3.13-slim

# Build-time proxy support (NOT persisted to runtime — internal Docker DNS must bypass proxy)
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY
ENV HTTP_PROXY=${HTTP_PROXY} \
    HTTPS_PROXY=${HTTPS_PROXY} \
    NO_PROXY=${NO_PROXY}

WORKDIR /app

# Copy docker CLI binary
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker

# Install system dependencies (apt honors *_PROXY env vars)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install Docker Compose plugin (curl honors *_PROXY env vars)
RUN mkdir -p /usr/local/lib/docker/cli-plugins && \
    curl -SL "https://github.com/docker/compose/releases/download/v2.40.3/docker-compose-linux-$(uname -m | sed 's/aarch64/aarch64/;s/x86_64/x86_64/')" -o /usr/local/lib/docker/cli-plugins/docker-compose && \
    chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

# Install Python dependencies (pip honors *_PROXY env vars)
RUN pip install --no-cache-dir \
    fastapi>=0.115.0 \
    uvicorn[standard]>=0.30.0 \
    sqlalchemy>=2.0.0 \
    alembic>=1.13.0 \
    python-jose[cryptography]>=3.3.0 \
    passlib[bcrypt]>=1.7.4 \
    bcrypt>=4.0.0 \
    httpx>=0.27.0 \
    docker>=7.0.0 \
    gitpython>=3.1.0 \
    cryptography>=42.0.0 \
    pyyaml>=6.0 \
    pydantic>=2.0.0 \
    pydantic-settings>=2.0.0 \
    aiofiles>=24.0.0 \
    python-multipart>=0.0.9

# Clear proxy settings — must NOT persist to runtime (internal Docker DNS would break)
ENV HTTP_PROXY="" \
    HTTPS_PROXY="" \
    NO_PROXY="" \
    http_proxy="" \
    https_proxy="" \
    no_proxy=""

# Copy application code
COPY app/ ./app/

# Create data directory
RUN mkdir -p /data

EXPOSE 8770

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8770"]
