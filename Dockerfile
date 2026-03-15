# ── Stage 1: build the C extension ──────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Build deps only (gcc, python headers)
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        python3-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Copy source and compile C extension
COPY binning/ binning/
COPY setup.py .
RUN pip install --no-cache-dir --prefix=/install setuptools && \
    python setup.py build_ext --inplace && \
    cp binning/_monotonic*.so /install/lib/python3.12/site-packages/binning/ 2>/dev/null || true


# ── Stage 2: lean runtime image ──────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source
COPY app.py          .
COPY binning/        binning/
COPY static/         static/
COPY templates/      templates/

# Copy compiled .so if it exists in binning/ (built in stage 1)
COPY --from=builder /build/binning/ binning/

# Non-root user for security
RUN useradd -m -u 1000 appuser && chown -R appuser /app
USER appuser

EXPOSE 5000

# Use gunicorn in production, flask dev server otherwise
ENV FLASK_ENV=production
ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "gunicorn", \
     "--bind", "0.0.0.0:5000", \
     "--workers", "4", \
     "--threads", "4", \
     "--timeout", "300", \
     "--worker-class", "gthread", \
     "app:app"]
