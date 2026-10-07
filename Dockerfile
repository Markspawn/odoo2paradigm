FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 converter \
    && useradd --uid 10001 --gid converter --no-create-home converter \
    && mkdir /data && chown converter:converter /data
COPY --chown=converter:converter converter.py catalog_update.py project_setup.py xls_export.py storage.py webapp.py odoo_api.py ./
COPY --chown=converter:converter templates ./templates
COPY --chown=converter:converter static ./static
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).read()"
CMD ["python", "webapp.py"]
