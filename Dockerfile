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

# Playwright chromium is NOT installed here — only needed for LinkedIn
# prospecting. Run 'python -m playwright install chromium --with-deps'
# from the Coolify terminal if you ever enable LinkedIn.

# Everything that must survive a redeploy -- config, .env, skills, prompts,
# the database and exports -- lives under SELLBIRD_HOME, which is a mounted
# volume. The image itself stays stateless, so nothing private is ever baked
# into it or pushed to the public repo. start.sh seeds this folder on first
# boot from the template files in /app.
ENV SELLBIRD_HOME=/config
RUN mkdir -p /config/data /config/exports /config/skills /config/prompts
VOLUME ["/config"]

EXPOSE 5555

COPY start.sh /start.sh
RUN chmod +x /start.sh

HEALTHCHECK --interval=30s --timeout=10s --start-period=90s \
  CMD curl -fs http://localhost:5555/ > /dev/null || exit 1

CMD ["/start.sh"]
