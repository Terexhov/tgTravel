FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/data

ENV METRICS_DB=/app/data/metrics.db

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import os,sys; sys.exit(0 if any('bot.py' in open(f'/proc/{p}/cmdline','rb').read().decode('utf-8','replace') for p in os.listdir('/proc') if p.isdigit()) else 1)"

CMD ["python", "bot.py"]
