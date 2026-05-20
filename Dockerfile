FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY metrics.py ./

# В dev docker-compose.yml монтирует .:/app поверх образа (bind mount + --reload в override).
# В prod используется готовый образ из реестра без volume.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
