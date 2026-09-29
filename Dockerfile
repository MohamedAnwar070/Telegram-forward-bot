# Dockerfile for Telegram forward bot (Pyrogram + MongoDB)

FROM python:3.11-slim

# Set work directory
WORKDIR /app

# Environment settings
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# System deps (for building wheels like TgCrypto / cryptography if needed)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Default command: run the bot
CMD ["python", "forward_bot.py"]
