# ──────────────────────────────────────────────────────────────────────────────
# AM API Gateway — Standalone Dockerfile
# This service is now a root-level repo, so it bundles the shared library
# directly from the local shared/ directory (no external build context needed).
# ──────────────────────────────────────────────────────────────────────────────
FROM python:3.11-slim

# Create a non-root user for security
RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser

WORKDIR /app

# Install dependencies first (layer caching optimisation)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Shared library is already inside this repo at ./shared/
# No need to copy from a parent context.

RUN chown -R appuser:appgroup /app
USER appuser

EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
