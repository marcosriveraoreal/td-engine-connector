FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    ACCEPT_EULA=Y \
    DEBIAN_FRONTEND=noninteractive

# Driver ODBC oficial de Microsoft (msodbcsql18). Necesario porque algunos SQL Server
# solo negocian correctamente con el driver oficial (igual que el JDBC de mssql-jdbc),
# no con FreeTDS.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl gnupg2 apt-transport-https ca-certificates unixodbc-dev build-essential \
    && curl -sSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    && curl -sSL https://packages.microsoft.com/config/debian/12/prod.list -o /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends msodbcsql18

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y --auto-remove curl gnupg2 build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY src/ ./src/
COPY sql/ ./sql/
COPY config/ ./config/
COPY assets/ ./assets/

RUN useradd --create-home --uid 10001 connector \
    && mkdir -p /app/state \
    && chown -R connector:connector /app

USER connector

ENV PYTHONPATH=/app/src \
    HEARTBEAT_FILE=/app/state/heartbeat \
    ASSETS_DIR=/app/assets

EXPOSE 3892

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python /app/src/healthcheck.py

ENTRYPOINT ["python", "/app/src/main.py"]
