# NSE intraday research + PAPER trading. No order endpoint exists in this image:
# the Groww client is wrapped in a market-data-only allowlist and execution_mode
# can only be "paper". Secrets are NOT baked in: they arrive at runtime as a
# Kubernetes Secret file (NSE_ENV_FILE).
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    NSE_ENV_FILE=/secrets/env

WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

COPY app ./app
COPY config ./config
COPY deploy/post_session.sh ./deploy/post_session.sh

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin nse \
    && mkdir -p /app/data && chown 10001:10001 /app/data \
    && chmod 0755 /app/deploy/post_session.sh

USER 10001
CMD ["python", "-m", "app.ops", "health"]
