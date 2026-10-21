FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends libglib2.0-0 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 app
WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app ./app
COPY scripts ./scripts
# models are downloaded at build time and checksum-verified (needs internet during the build)
RUN python scripts/download_models.py && chown -R app /srv
USER app
EXPOSE 8101
HEALTHCHECK --interval=30s CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://localhost:8101/health').status==200 else 1)"
# ONE worker per container: the OpenCV models are guarded by a lock, so scale with replicas, not workers
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8101", "--workers", "1"]
