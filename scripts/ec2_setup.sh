#!/bin/bash
# EC2 bootstrap script for TradingAgent
# Run once after launching a fresh Ubuntu instance (24.04 or 26.04).
#
# Usage:
#   chmod +x scripts/ec2_setup.sh
#   ./scripts/ec2_setup.sh
#
# Assumes:
#   - EC2 instance has an IAM role with s3:GetObject on amzn-s3-somal-bucket
#   - Git repo already cloned into this directory
#   - You will fill in .env manually after this script runs

set -e   # exit immediately on any error

S3_BUCKET="${1:-amzn-s3-somal-bucket}"
S3_PREFIX="${2:-tradingagent}"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "==========================================="
echo "  TradingAgent EC2 Setup"
echo "  Repo  : $REPO_DIR"
echo "  Bucket: s3://$S3_BUCKET/$S3_PREFIX"
echo "==========================================="

# ── 1. System packages ────────────────────────
echo ""
echo "[1/6] Installing system packages..."
sudo apt-get update -q
sudo apt-get install -y -q python3 python3-venv python3-dev python3-pip tmux git awscli

# ── 2. Python virtual environment ─────────────
echo ""
echo "[2/6] Creating Python virtual environment..."
cd "$REPO_DIR"
PYTHON=$(which python3)
echo "  Using Python: $($PYTHON --version)"
$PYTHON -m venv venv
source venv/bin/activate

# ── 3. Install Python dependencies ────────────
echo ""
echo "[3/6] Installing Python dependencies (this takes ~3 min)..."
pip install --quiet --upgrade pip
# Use requirements-ec2.txt (strips vectorbt/numba which don't support Python 3.14)
REQ_FILE="requirements-ec2.txt"
[ -f "$REQ_FILE" ] || REQ_FILE="requirements.txt"
echo "  Using: $REQ_FILE"
pip install --quiet -r "$REQ_FILE"

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
echo "[5/6] Downloading historical data from s3://$S3_BUCKET/$S3_PREFIX  (~1.7 GB, using aws s3 sync)..."
mkdir -p data/stocks data/index
aws s3 sync "s3://$S3_BUCKET/$S3_PREFIX/data/stocks/" data/stocks/
aws s3 sync "s3://$S3_BUCKET/$S3_PREFIX/data/index/"  data/index/

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
    print('  All good — data looks complete.')
"

echo ""
echo "==========================================="
echo "  Setup complete!"
echo ""
echo "  Start the full 3-4 day pipeline:"
echo "    tmux new -s pipeline"
echo "    source venv/bin/activate"
echo "    python run_full_pipeline.py --dry-run   # preview"
echo "    python run_full_pipeline.py             # start"
echo "    (Ctrl+B then D to detach)"
echo ""
echo "  Watch progress from anywhere:"
echo "    tail -f logs/pipeline_*.log"
echo "    tail -f logs/analysis_2016.log"
echo "==========================================="
