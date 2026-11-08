# syntax=docker/dockerfile:1
# Uma imagem para todos os servidores MCP do repo; o compose escolhe qual roda:
#   command: ["src.servers.trainingpeaks"]
# Build: `mise deploy` (raiz do repo) ou `docker buildx build --platform linux/arm64 -t mcp .`

# ---------- dependências ----------
FROM python:3.14-slim AS python-deps
RUN pip install --no-cache-dir "poetry>=2,<3"
WORKDIR /app
COPY pyproject.toml poetry.lock ./
ENV VIRTUAL_ENV=/venv PATH=/venv/bin:$PATH
RUN python -m venv /venv \
    && poetry install --only main --no-root --no-interaction --no-ansi

# ---------- runtime ----------
FROM python:3.14-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/venv/bin:$PATH \
    MCP_TRANSPORT=streamable-http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000
COPY --from=python-deps /venv /venv
WORKDIR /app
# Testes e fixtures não vão para a imagem (.dockerignore)
COPY --chmod=a+rX src ./src
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"]
ENTRYPOINT ["python", "-m"]
