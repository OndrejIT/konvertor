FROM python:3.12-slim

# ffmpeg/ffprobe jsou systémová závislost, ne Python balíček.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# uv binárka z oficiálního image, pinnutá verze kvůli reprodukovatelnosti.
COPY --from=ghcr.io/astral-sh/uv:0.11.28 /uv /uvx /usr/local/bin/

WORKDIR /app

ENV UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:${PATH}"

# Nejdřív jen závislosti - ať se při změně kódu nemusí reinstalovat.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev

COPY app ./app
COPY static ./static

# Neroot uživatel - kontejner běží bez rootích práv.
RUN useradd --create-home --shell /usr/sbin/nologin appuser
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/config', timeout=4)"]

# config.toml se do image nekopíruje (obsahuje tajné heslo) - je nutné ho
# při běhu namountovat, viz README.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
