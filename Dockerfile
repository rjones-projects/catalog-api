# ── Stage 1: build deps ───────────────────────────────────────────────────────
FROM python:3.12-alpine AS builder

# Build tools needed for some C-extension wheels
RUN apk add --no-cache gcc musl-dev libffi-dev

WORKDIR /build

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM python:3.12-alpine AS runtime

LABEL org.opencontainers.image.title="terraform-resolver" \
      org.opencontainers.image.description="REST API that resolves Terraform modules and generates main.tf + variables.tf" \
      org.opencontainers.image.version="1.0.0"

# Non-root user for security
RUN addgroup -S resolver && adduser -S resolver -G resolver

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source
COPY app/ ./app/

USER resolver

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD wget -qO- http://localhost:8080/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "2"]
