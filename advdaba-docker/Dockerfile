FROM python:3.9-slim

# Install dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
#COPY ultra_fast_loader.py .
COPY main2.py .

# Create logs directory
RUN mkdir -p /app/logs

# Default command
CMD ["python", "main2.py"]