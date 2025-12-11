#!/usr/bin/env bash
set -xe

# Install playwright browsers
pip install playwright
playwright install chromium

# Install required system packages for Chromium
apt-get update
apt-get install -y wget gnupg ca-certificates fonts-liberation libappindicator3-1 libasound2 \
    libatk-bridge2.0-0 libatk1.0-0 libcups2 libnss3 libxcomposite1 libxdamage1 \
    libxrandr2 xdg-utils libxkbcommon0 libxshmfence1 libgbm1 libgtk-3-0 libx11-6

echo "Render build step completed"
