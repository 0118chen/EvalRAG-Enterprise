FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY docker-entrypoint.sh /usr/local/bin/evalrag-entrypoint
RUN pip install --no-cache-dir .
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data/uploads \
    && chown -R appuser:appuser /app \
    && chmod +x /usr/local/bin/evalrag-entrypoint
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=3)"
ENTRYPOINT ["evalrag-entrypoint"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
