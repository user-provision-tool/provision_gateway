# provision-gateway Dockerfile
# Multi-stage: copy docker CLI from docker:cli image

FROM docker:cli AS docker-cli

FROM python:3.13-slim

WORKDIR /app

# Copy docker CLI binary
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
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
    aiofiles>=24.0.0

# Copy application code
COPY app/ ./app/

# Create data directory
RUN mkdir -p /data

EXPOSE 8770

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8770"]
