# Use Python 3.11 slim image
FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV DEBIAN_FRONTEND=noninteractive
ENV WDM_LOCAL=1
ENV WDM_LOG_LEVEL=0

# Install system dependencies
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    unzip \
    curl \
    ca-certificates \
    gosu \
    nano \
    xvfb \
    xauth \
    libglib2.0-0 \
    libnss3 \
    libfontconfig1 \
    tesseract-ocr \
    tesseract-ocr-eng \
    # Dependencies for Chrome
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libatspi2.0-0 \
    libcups2 \
    libdrm2 \
    libgbm1 \
    libgtk-3-0 \
    libnspr4 \
    libx11-xcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    libxss1 \
    && rm -rf /var/lib/apt/lists/*

# Install Chromium (supports both AMD64 and ARM64)
RUN apt-get update \
    && apt-get install -y chromium chromium-driver \
    && rm -rf /var/lib/apt/lists/* \
    # Create symlinks for compatibility
    && ln -s /usr/bin/chromium /usr/bin/google-chrome || true \
    && ln -s /usr/bin/chromium /usr/bin/google-chrome-stable || true

# Set work directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Create non-root user for security (will be updated by entrypoint)
RUN groupadd -g 1000 appuser && useradd -u 1000 -g appuser -m appuser

# Copy application code
COPY . .

# Set up entrypoint script
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Create directories for data persistence and webdriver cache
RUN mkdir -p /app/data /app/logs /app/.wdm

# Set HOME directory for webdriver cache
ENV HOME=/app

# Expose port
EXPOSE 1851

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:1851/api/status || exit 1

# Set entrypoint and default command
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python3", "-m", "gunicorn", "--bind", "0.0.0.0:1851", "--workers", "1", "--timeout", "600", "wsgi:app"]
