FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV FUND_DB_PATH=/data/fund_tracker.sqlite3
ENV FUND_APP_HOST=0.0.0.0
ENV FUND_APP_PORT=8765

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py serve.py ./
COPY templates ./templates
COPY static ./static

VOLUME ["/data"]
EXPOSE 8765

CMD ["python", "serve.py"]
