FROM python:3.12-slim AS base

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl procps ipmitool && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY README.md ./

# Install dependencies (without building)
RUN pip install --no-cache-dir . 2>/dev/null || true
RUN pip install --no-cache-dir prometheus-client pynvml pyrsmi PyYAML

# Copy application code
COPY gpu_exporter/ ./gpu_exporter/

EXPOSE 9102

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:9102/health || exit 1

CMD ["python", "-m", "gpu_exporter.main"]
