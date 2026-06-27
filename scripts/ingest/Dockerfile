FROM python:3.11-slim
WORKDIR /app
RUN pip install requests --no-cache-dir
COPY ingest_x.py scheduler.py ./
CMD ["python", "scheduler.py"]
