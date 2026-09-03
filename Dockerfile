FROM python:3.11-slim
WORKDIR /app

# Install system deps for image handling and Tesseract (optional)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libgl1 \
    libsm6 \
    libxrender1 \
    tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

CMD ["uvicorn", "engine.app:app", "--host", "0.0.0.0", "--port", "8000"]
