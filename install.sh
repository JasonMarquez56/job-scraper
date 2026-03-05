#!/bin/bash

set -e

echo "==> Installing system dependencies..."
sudo apt-get update -qq
sudo apt-get install -y python3-pip python3-venv

echo "==> Creating virtual environment..."
python3 -m venv venv
source venv/bin/activate

echo "==> Installing Python packages..."
pip install --upgrade pip
pip install -r requirements.txt

echo "==> Setting up cron job (runs daily at 8:00 AM)..."
SCRIPT_DIR="$(pwd)"
CRON_CMD="0 8 * * * cd $SCRIPT_DIR && $SCRIPT_DIR/venv/bin/python scraper.py >> $SCRIPT_DIR/scraper.log 2>&1"

# Add to crontab if not already present
(crontab -l 2>/dev/null | grep -qF "scraper.py") || \
    (crontab -l 2>/dev/null; echo "$CRON_CMD") | crontab -

echo ""
echo "Setup complete!"
echo ""
echo "Next steps:"
echo "  1. Create a .env file with your Discord webhook: DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...""
echo "  2. Test a run now:  source venv/bin/activate && python scraper.py"
echo "  3. The cron job will run every day at 8:00 AM automatically"
echo ""
echo "To change the schedule, run: crontab -e"
echo "  Examples:"
echo "    0 8 * * *   = 8:00 AM daily"
echo "    0 8,17 * * * = 8 AM and 5 PM daily"
echo "    */30 * * * * = every 30 minutes (not recommended)"