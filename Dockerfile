FROM python:3.12-slim

WORKDIR /app

# Install deps first (cache layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY app/ app/
COPY run.py .
COPY .env .env

# Create DB dir
RUN mkdir -p /app/app/db

EXPOSE 8090

CMD ["python", "run.py"]
