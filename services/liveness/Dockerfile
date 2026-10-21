FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends libglib2.0-0 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 app
WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app ./app
COPY scripts ./scripts
COPY models ./models
# the two MiniFASNet ONNX files ship in the repo; only the small YuNet detector is downloaded (checksum-verified)
RUN python scripts/download_models.py && chown -R app /srv
USER app
EXPOSE 8102
HEALTHCHECK --interval=30s CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://localhost:8102/health').status==200 else 1)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8102", "--workers", "1"]
