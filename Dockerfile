FROM python:3.12-slim

WORKDIR /app

# System deps for Playwright chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl git npm \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js for Claude Code CLI
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

COPY . .

RUN pip install --no-cache-dir -e .

# Install Playwright browser
RUN python -m playwright install chromium --with-deps

# Persistent-data directories (overridden by mounted volumes)
RUN mkdir -p data exports

EXPOSE 5555

COPY start.sh /start.sh
RUN chmod +x /start.sh

HEALTHCHECK --interval=30s --timeout=10s --start-period=90s \
  CMD curl -fs http://localhost:5555/ > /dev/null || exit 1

CMD ["/start.sh"]
