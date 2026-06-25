#!/bin/bash
# EC2 bootstrap script for TradingAgent
# Run once after launching a fresh Ubuntu 24.04 instance.
#
# Usage:
#   chmod +x scripts/ec2_setup.sh
#   ./scripts/ec2_setup.sh YOUR_S3_BUCKET
#
# Assumes:
#   - EC2 instance has an IAM role with s3:GetObject on YOUR_S3_BUCKET
#   - Git repo already cloned (or run: git clone https://github.com/somalkant/TRAIAGENT.git)
#   - You will fill in .env manually after this script runs

set -e   # exit immediately on any error

S3_BUCKET="${1:-amzn-s3-somal-bucket}"
S3_PREFIX="${2:-tradingagent}"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "==========================================="
echo "  TradingAgent EC2 Setup"
echo "  Repo  : $REPO_DIR"
echo "  Bucket: s3://$S3_BUCKET"
echo "==========================================="

# ── 1. System packages ────────────────────────
echo ""
echo "[1/6] Installing system packages..."
sudo apt-get update -q
sudo apt-get install -y -q python3.12 python3.12-venv python3.12-dev tmux git

# ── 2. Python virtual environment ─────────────
echo ""
echo "[2/6] Creating Python virtual environment..."
cd "$REPO_DIR"
python3.12 -m venv venv
source venv/bin/activate

# ── 3. Install Python dependencies ────────────
echo ""
echo "[3/6] Installing Python dependencies (this takes ~3 min)..."
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

# ── 4. Environment file ────────────────────────
echo ""
echo "[4/6] Setting up .env file..."
if [ ! -f .env ]; then
    cp .env.example .env
    echo ""
    echo "  ⚠  .env created from template — EDIT IT NOW with your real API keys:"
    echo "     nano $REPO_DIR/.env"
    echo ""
    echo "  Press ENTER after you have saved .env to continue..."
    read -r
else
    echo "  .env already exists — skipping"
fi

# ── 5. Download data from S3 ──────────────────
echo ""
echo "[5/6] Downloading historical data from s3://$S3_BUCKET/$S3_PREFIX  (~1.7 GB)..."
python -m data_pipeline.s3_sync download --bucket "$S3_BUCKET" --prefix "$S3_PREFIX"

# ── 6. Quick sanity check ─────────────────────
echo ""
echo "[6/6] Sanity check..."
python -c "
from pathlib import Path
stocks = list(Path('data/stocks').rglob('*.parquet'))
index  = list(Path('data/index').rglob('*.parquet'))
print(f'  data/stocks : {len(stocks)} parquet files')
print(f'  data/index  : {len(index)} parquet files')
if len(stocks) < 100:
    print('  WARNING: fewer files than expected — check S3 sync')
else:
    print('  Data looks good.')
"

echo ""
echo "==========================================="
echo "  Setup complete!"
echo ""
echo "  To start the live agent in a tmux session:"
echo "    tmux new -s live"
echo "    source venv/bin/activate"
echo "    python live/agent.py"
echo "    (Ctrl+B then D to detach)"
echo ""
echo "  To run backtests:"
echo "    source venv/bin/activate"
echo "    python run_analysis.py --year 2023"
echo ""
echo "  To watch logs:"
echo "    tail -f logs/live_\$(date +%Y-%m-%d).log"
echo "==========================================="
