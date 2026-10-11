FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && adduser --system --uid 10001 --group app

COPY main.py catalog.py models.py ordinet.py tools.py returns.py retrieval.py ./
COPY data/gift-shop-catalog.csv data/search-vectors.json ./data/
COPY frontend/index.html ./frontend/index.html

USER app
EXPOSE 8000

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
