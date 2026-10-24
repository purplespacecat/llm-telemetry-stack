FROM python:3.13-slim

WORKDIR /app
COPY exporter.py /app/exporter.py

ENV PYTHONUNBUFFERED=1
RUN install -d -m 0777 /state
USER 1000:1000
ENTRYPOINT ["python3", "/app/exporter.py"]
