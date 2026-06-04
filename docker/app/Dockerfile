FROM python:3.12-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv
RUN pip install --no-cache-dir uv

# Set working directory
WORKDIR /app

# Copy dependency files and source code (needed for editable install)
COPY pyproject.toml ./
COPY src ./src

# Install dependencies (package installed in editable mode)
RUN uv pip install --system -e ".[dev]"

# Copy remaining files
COPY migrations ./migrations
COPY alembic ./alembic
COPY alembic.ini ./
COPY templates ./templates
COPY static ./static

# Expose port
EXPOSE 8000

# Run application
CMD ["sh", "-c", "uvicorn submissions_checker.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
