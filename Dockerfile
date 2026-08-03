# syntax=docker/dockerfile:1
FROM python:3.13-slim

# Keep the package manager version explicit and resolve runtime dependencies
# from the committed lock file.
COPY --from=ghcr.io/astral-sh/uv:0.12.0 /uv /uvx /bin/

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY red_transporte_mcp/ ./red_transporte_mcp/

RUN uv sync --locked --no-dev \
    && groupadd --system app \
    && useradd --system --gid app --home /app app \
    && chown -R app:app /app

USER app
ENV PYTHONUNBUFFERED=1

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/health')"

ENTRYPOINT ["/app/.venv/bin/red-transporte-mcp", "--transport", "http", "--host", "0.0.0.0", "--port", "8001"]
