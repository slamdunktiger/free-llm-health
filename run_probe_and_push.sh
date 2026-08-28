#!/bin/bash
# Free LLM Health Probe — hourly cron wrapper
# Runs probe, generates reports, pushes to GitHub

set -e

PROBE_DIR="/root/free-llm-probe"
REPORTS_DIR="$PROBE_DIR/reports"
TODAY=$(date -u +%Y-%m-%d)
LOG="/var/log/free-llm-probe.log"

echo "=== $(date -u) ===" >> "$LOG"
echo "Starting probe run..." >> "$LOG"

cd "$PROBE_DIR"

# Run the probe
python3 probe.py >> "$LOG" 2>&1
echo "Probe complete" >> "$LOG"

# Run daily report at end of day (23:00 UTC)
HOUR=$(date -u +%H)
if [ "$HOUR" = "23" ]; then
    echo "Generating daily report..." >> "$LOG"
    python3 probe.py --report >> "$LOG" 2>&1
fi

# Push to GitHub
echo "Pushing to GitHub..." >> "$LOG"

# Configure git to use deploy key
export GIT_SSH_COMMAND="ssh -i /root/.ssh/free-llm-probe -o StrictHostKeyChecking=accept-new"

# Clone if needed
if [ ! -d ".git" ]; then
    git clone git@github.com:slamdunktiger/free-llm-health.git repo-temp >> "$LOG" 2>&1
    mv repo-temp/.git .git
    rm -rf repo-temp
    git config user.email "probe@138.197.207.62"
    git config user.name "Free LLM Probe"
fi

# Pull latest
git pull --rebase >> "$LOG" 2>&1 || true

# Copy current.json
cp "$REPORTS_DIR/current.json" .

# Copy report if it exists
if [ -f "$REPORTS_DIR/${TODAY}.md" ]; then
    mkdir -p reports
    cp "$REPORTS_DIR/${TODAY}.md" reports/
fi

# Commit and push
git add -A
git commit -m "Health update — ${TODAY} $(date -u +%H:00 UTC)" >> "$LOG" 2>&1 || true
git push origin main >> "$LOG" 2>&1

echo "Push complete" >> "$LOG"
echo "=== Done ===" >> "$LOG"
