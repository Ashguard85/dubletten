FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DB_PATH=/app/data/crm.sqlite \
    HOME=/tmp \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_SERVER_MAX_UPLOAD_SIZE=60 \
    STREAMLIT_SERVER_MAX_MESSAGE_SIZE=90 \
    STREAMLIT_SERVER_ENABLE_CORS=true \
    STREAMLIT_SERVER_ENABLE_XSRF_PROTECTION=true
WORKDIR /opt/app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/data /app/backups \
    && chown -R 10001:10001 /app /opt/app
COPY --chown=10001:10001 app/ ./
# Streamlit settings are env-based: no hidden .streamlit folder required in Git.
USER 10001:10001
EXPOSE 8501
CMD ["streamlit", "run", "ui.py", "--server.address=0.0.0.0", "--server.port=8501"]
