#!/bin/bash
# Free LLM Health Probe — hourly cron wrapper
# Runs probe, generates reports, pushes to GitHub
# Designed to be run from /root/free-llm-probe via cron

set -e

PROBE_DIR="/root/free-llm-probe"
REPORTS_DIR="$PROBE_DIR/reports"
TODAY=$(date -u +%Y-%m-%d)
LOG="/var/log/free-llm-probe.log"

echo "=== $(date -u -Iseconds) ===" >> "$LOG"
echo "Starting probe run..." >> "$LOG"

cd "$PROBE_DIR"

# Ensure git repo is set up
export GIT_SSH_COMMAND="ssh -i /root/.ssh/free-llm-probe -o StrictHostKeyChecking=accept-new"

if [ ! -d ".git" ]; then
    echo "Initializing git repo..." >> "$LOG"
    git init
    git remote add origin git@github.com:slamdunktiger/free-llm-health.git 2>/dev/null || true
    git fetch origin main
    git checkout -b main --track origin/main 2>/dev/null || git checkout main 2>/dev/null || true
    git config user.email "probe@138.197.207.62"
    git config user.name "Free LLM Probe"
fi

# Pull latest (discard any local-only changes since they're regenerable)
git add -A
git commit -m "Auto-save before pull $(date -u +%Y%m%d%H%M)" >> "$LOG" 2>&1 || true
git fetch origin main
git reset --hard origin/main >> "$LOG" 2>&1

# Run the probe
echo "Running probe..." >> "$LOG"
python3 probe.py >> "$LOG" 2>&1
echo "Probe complete" >> "$LOG"

# Generate daily report at end of day (23:00 UTC)
HOUR=$(date -u +%H)
if [ "$HOUR" = "23" ]; then
    echo "Generating daily report..." >> "$LOG"
    python3 probe.py --report >> "$LOG" 2>&1
fi

# Copy files to repo root
cp "$REPORTS_DIR/current.json" .

# Copy report if it exists
if [ -f "$REPORTS_DIR/${TODAY}.md" ]; then
    mkdir -p reports
    cp "$REPORTS_DIR/${TODAY}.md" reports/
fi

# Commit and push
echo "Pushing to GitHub..." >> "$LOG"
git add -A
git commit -m "Health update — ${TODAY} $(date -u +%H):00 UTC" >> "$LOG" 2>&1 || true
git push origin main >> "$LOG" 2>&1

echo "Push complete" >> "$LOG"
echo "=== Done ===" >> "$LOG"
