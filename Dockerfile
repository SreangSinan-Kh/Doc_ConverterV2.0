FROM python:3.12.1-slim

# ដំឡើងកម្មវិធីជំនួយ (OS Dependencies) ទាំងអស់ដែល Bot ត្រូវការមិនឱ្យចន្លោះ
RUN apt-get update && apt-get install -y \
    ffmpeg \
    tesseract-ocr \
    tesseract-ocr-eng \
    tesseract-ocr-khm \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# កំណត់ទីតាំងធ្វើការ
WORKDIR /app

# ចម្លង និងដំឡើង Library របស់ Python ទាំងអស់ពី requirements.txt
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ចម្លងកូដទាំងមូលចូលក្នុងម៉ាស៊ីន
COPY . .

# បញ្ឆេះ Bot របស់អ្នក
CMD ["python", "main.py"]
