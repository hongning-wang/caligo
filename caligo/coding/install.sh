set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
    bash ca-certificates curl git less python3 python3-pip python3-venv \
    ripgrep tmux vim
rm -rf /var/lib/apt/lists/*
npm install --global @openai/codex@0.164.0 @anthropic-ai/claude-code@2.1.310
npm cache clean --force
