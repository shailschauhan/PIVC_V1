FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends libglib2.0-0 && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY requirements.txt .
# CPU-only torch keeps the image small; use a CUDA base image + default torch wheels for GPU workers
RUN pip install --extra-index-url https://download.pytorch.org/whl/cpu -r requirements.txt \n && pip install --no-deps open-rppg==0.1.1
COPY app ./app
COPY age_worker ./age_worker
COPY scripts ./scripts
# age estimation runs in its own virtualenv (conflicting pins); models are mounted at /models/age
RUN python -m venv /srv/.venv-age && /srv/.venv-age/bin/pip install -r age_worker/requirements.txt
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://localhost:8000/api/health').status==200 else 1)"
# one process: the in-process analysis queue and the loaded models live in it (scale with replicas, not workers)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
