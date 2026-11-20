FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DB_PATH=/app/data/crm.sqlite \
    HOME=/tmp
WORKDIR /opt/app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/data /app/backups \
    && chown -R 10001:10001 /app /opt/app
COPY --chown=10001:10001 app/ ./
COPY --chown=10001:10001 .streamlit/ ./.streamlit/
USER 10001:10001
EXPOSE 8501
CMD ["streamlit", "run", "ui.py", "--server.address=0.0.0.0", "--server.port=8501"]
