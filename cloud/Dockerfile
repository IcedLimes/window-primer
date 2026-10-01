# window-primer cloud runner: Railway cron starts this, it pings if a planned time is due, then exits.
FROM node:22-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

# Pinned to the version whose stream-json rate_limit_event format the parser was tested against.
ARG CLAUDE_CODE_VERSION=2.1.280
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} && claude --version

WORKDIR /app
COPY primer/ /app/primer/
USER node
ENV HOME=/home/node PYTHONUNBUFFERED=1
CMD ["python3", "-m", "primer.cloud"]
