FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libjpeg62-turbo && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY worker.py telemetry_worker.py ./
ENV DATA_DIR=/data
VOLUME ["/data"]
CMD ["python", "worker.py"]