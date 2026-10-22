FROM python:3.13-slim

WORKDIR /app
COPY exporter.py /app/exporter.py

ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python3", "/app/exporter.py"]
