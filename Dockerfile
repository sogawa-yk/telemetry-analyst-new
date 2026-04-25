# API バックエンド用イメージ (ta-agent / port 8080)
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# ソース一式を先に配置 (setuptools のパッケージ検出に src/ が必要)
COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY skills/ ./skills/
COPY memory/ ./memory/

RUN pip install --upgrade pip && pip install .

# 非 root ユーザで起動
RUN useradd -u 10001 -m ta && chown -R ta:ta /app
USER ta

EXPOSE 8080

CMD ["python", "-m", "uvicorn", "ta.api.main:app", "--host", "0.0.0.0", "--port", "8080"]
