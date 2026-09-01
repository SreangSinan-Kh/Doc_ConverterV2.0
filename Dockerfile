FROM python:3.12.1-slim

# ដំឡើងកម្មវិធីប្រព័ន្ធ (ថែម unrar សម្រាប់ដោះកូដ RAR)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    tesseract-ocr \
    tesseract-ocr-eng \
    tesseract-ocr-khm \
    poppler-utils \
    unrar \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]
