FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /data
ENV ATLAS_DB_PATH=/data/atlas.db PORT=8000 ATLAS_ENV=production
CMD ["sh", "-c", "uvicorn atlas.asgi:app --host 0.0.0.0 --port ${PORT}"]
