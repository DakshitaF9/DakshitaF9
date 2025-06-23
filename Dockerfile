# Use official Python as a base image
FROM python:3.10-slim

# Set the working directory
WORKDIR /app

# Install system dependencies for reportlab and matplotlib
RUN apt-get update && apt-get install -y \
    build-essential \
    libfreetype6-dev \
    libxrender1 \
    libxext6 \
    libx11-dev \
    libjpeg-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy all files into the container
COPY . .

# Install Python dependencies
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

# Expose Streamlit's default port
EXPOSE 8501

# Set the entry command
CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]
