FROM node:22-bookworm-slim
ARG CODEX_VERSION=0.163.0
ARG CLAUDE_SDK_VERSION=0.2.168
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv git ca-certificates bash \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/agent-venv \
    && /opt/agent-venv/bin/pip install --no-cache-dir claude-agent-sdk==${CLAUDE_SDK_VERSION} \
    && npm install -g @openai/codex@${CODEX_VERSION}
ENV PATH="/opt/agent-venv/bin:${PATH}"
