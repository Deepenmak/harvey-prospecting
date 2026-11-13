FROM python:3.12-slim

WORKDIR /app

# Keep this layer as small as possible. The deployment server's Debian mirror
# is slow and flaky (a build there fetched at 52 kB/s and the connection died
# mid-download), so every megabyte is another chance for the build to fail.
#
# npm is deliberately NOT installed from apt. That package pulls in 405
# packages and 69 MB -- eslint, webpack, babel, terser, libnode-dev and a
# whole second Python interpreter -- and it was what killed the build. It was
# always redundant too, because Node ships npm itself. Node is fetched here as
# one official tarball instead of through an apt repository, which also avoids
# needing gnupg and a second apt-get update.
ENV NODE_VERSION=20.18.1
RUN set -eux; \
    echo 'Acquire::Retries "5";' > /etc/apt/apt.conf.d/80-retries; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl ca-certificates git xz-utils; \
    rm -rf /var/lib/apt/lists/*; \
    case "$(dpkg --print-architecture)" in \
      amd64) node_arch=x64 ;; \
      arm64) node_arch=arm64 ;; \
      *) echo "unsupported architecture: $(dpkg --print-architecture)" >&2; exit 1 ;; \
    esac; \
    curl -fsSL --retry 5 --retry-delay 3 --retry-connrefused \
      "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${node_arch}.tar.xz" \
      -o /tmp/node.tar.xz; \
    tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1; \
    rm /tmp/node.tar.xz; \
    node --version; \
    npm --version

# Claude Code CLI, which the heartbeat shells out to for every brain call.
RUN npm install -g --no-audit --no-fund @anthropic-ai/claude-code \
    && npm cache clean --force

# Dependencies first, from requirements.txt alone, so this layer is cached
# and a code change never re-downloads them. pip's default 15s socket timeout
# and 5 retries are not enough on this deployment server's network, so both
# are raised: a slow mirror now waits instead of failing the whole build.
COPY requirements.txt ./
RUN pip install --no-cache-dir --retries 10 --timeout 120 -r requirements.txt

COPY . .

# The project itself. --no-deps because requirements.txt already installed
# them above, so this touches the network not at all.
RUN pip install --no-cache-dir --no-deps -e .

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
