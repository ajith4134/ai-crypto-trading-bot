FROM python:3.11-slim

WORKDIR /app

RUN groupadd -r botuser && useradd -r -g botuser botuser

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# cont. 47 — install docker CLI + compose plugin so the watchdog can
# `docker compose up -d --force-recreate <svc>` when a mode switch is
# requested (env_file vars only reload on container CREATION, not restart).
# Placed AFTER pip install so the heavy pip layer stays cached when this
# is added. Adds ~80 MB; only the watchdog uses the CLI at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates gnupg && \
    install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://download.docker.com/linux/debian/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg && \
    chmod a+r /etc/apt/keyrings/docker.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        > /etc/apt/sources.list.d/docker.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin && \
    rm -rf /var/lib/apt/lists/*

COPY . .

RUN chown -R botuser:botuser /app

USER botuser
