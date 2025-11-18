# Unified Dockerfile for all Celery fork-safety testing scenarios
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# Install system dependencies for debugging and tools
RUN apt-get update && apt-get install -y \
    # Debugging tools
    gdb \
    strace \
    lsof \
    procps \
    # Database
    redis-tools \
    # Clean up
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy dependency files
COPY pyproject.toml uv.lock ./

# Install all Python dependencies using uv
RUN uv sync --frozen --no-dev

# Add the virtual environment to PATH
ENV PATH="/app/.venv/bin:$PATH"

# Set PYTHONPATH to ensure modules can be imported
ENV PYTHONPATH="/app/celery_examples:/app"

# Copy application code
COPY scripts/ ./scripts/
COPY celery_examples/ ./celery_examples/

# Default command (can be overridden)
CMD ["bash"]

