"""
S3 sync for trading agent historical data (data/stocks/ + data/index/).

Upload from local machine (one-time, ~1.7 GB):
    python -m data_pipeline.s3_sync upload --bucket amzn-s3-somal-bucket --prefix tradingagent

Download on EC2 (one-time setup):
    python -m data_pipeline.s3_sync download --bucket amzn-s3-somal-bucket --prefix tradingagent

Status check:
    python -m data_pipeline.s3_sync status --bucket amzn-s3-somal-bucket --prefix tradingagent

S3 layout:
    s3://amzn-s3-somal-bucket/tradingagent/data/stocks/2016/RELIANCE.parquet
    s3://amzn-s3-somal-bucket/tradingagent/data/index/2016/NIFTY50.parquet

Credentials:
  - EC2:   attach an IAM role with s3:GetObject + s3:PutObject on the bucket (no keys needed)
  - Local: run `aws configure` once, or set AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY in .env
"""

import argparse
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import boto3
from tqdm import tqdm

from config.settings import DATA_DIR, STOCKS_DIR, INDEX_DIR

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s", datefmt="%H:%M:%S")

MAX_WORKERS = 16   # parallel S3 transfers; S3 is I/O bound so threading scales well

S3_BUCKET  = "amzn-s3-somal-bucket"
S3_PREFIX  = "tradingagent"           # folder inside the bucket


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC COMMANDS
# ─────────────────────────────────────────────────────────────────────────────

def upload(bucket: str, prefix: str) -> None:
    """Upload data/stocks/ and data/index/ to S3. Skips files already in sync."""
    s3 = boto3.client("s3")
    local_files = _collect_local_files(prefix)
    if not local_files:
        log.warning("No local parquet files found under data/stocks/ or data/index/")
        return

    log.info(f"Uploading {len(local_files)} files → s3://{bucket}/{prefix}/  ({MAX_WORKERS} parallel)")
    existing = _list_s3_sizes(s3, bucket, prefix)
    to_upload = [
        (local, key) for local, key in local_files
        if existing.get(key) != local.stat().st_size
    ]
    skipped = len(local_files) - len(to_upload)
    log.info(f"  {skipped} already in sync, {len(to_upload)} to upload")

    _run_parallel(
        items=to_upload,
        worker=lambda args: _upload_one(s3, bucket, args[0], args[1]),
        desc="Uploading",
    )
    log.info("Upload complete.")


def download(bucket: str, prefix: str) -> None:
    """Download data/stocks/ and data/index/ from S3 to local. Skips files already in sync."""
    s3 = boto3.client("s3")
    s3_files = _list_s3_objects(s3, bucket, prefix)
    if not s3_files:
        log.warning(f"No parquet files found in s3://{bucket}/{prefix}/data/")
        return

    log.info(f"Downloading {len(s3_files)} files from s3://{bucket}/{prefix}/  ({MAX_WORKERS} parallel)")
    to_download = []
    s3_data_prefix = f"{prefix}/data/" if prefix else "data/"
    for key, s3_size in s3_files:
        # strip prefix to get relative path e.g. "data/stocks/2016/RELIANCE.parquet"
        relative = key[len(s3_data_prefix):]           # "stocks/2016/RELIANCE.parquet"
        local = DATA_DIR / relative
        if not local.exists() or local.stat().st_size != s3_size:
            to_download.append((key, local, s3_size))

    skipped = len(s3_files) - len(to_download)
    log.info(f"  {skipped} already in sync, {len(to_download)} to download")

    _run_parallel(
        items=to_download,
        worker=lambda args: _download_one(s3, bucket, args[0], args[1]),
        desc="Downloading",
    )
    log.info("Download complete.")


def status(bucket: str, prefix: str) -> None:
    """Compare local vs S3 file counts and sizes."""
    s3 = boto3.client("s3")
    local_files = _collect_local_files(prefix)
    s3_files    = _list_s3_objects(s3, bucket, prefix)
    s3_index    = {key: size for key, size in s3_files}

    local_size = sum(p.stat().st_size for p, _ in local_files)
    s3_size    = sum(s3_index.values())
    in_sync    = sum(1 for p, key in local_files if s3_index.get(key) == p.stat().st_size)

    print(f"\n{'─'*50}")
    print(f"  Local  : {len(local_files):>5} files  ({local_size/1e9:.2f} GB)")
    print(f"  S3     : {len(s3_files):>5} files  ({s3_size/1e9:.2f} GB)")
    print(f"  In sync: {in_sync:>5} files")
    print(f"  Need upload  : {len(local_files) - in_sync}")
    print(f"  Only in S3   : {len(s3_files) - in_sync}")
    print(f"  URI    : s3://{bucket}/{prefix}/")
    print(f"{'─'*50}\n")


# ─────────────────────────────────────────────────────────────────────────────
# INTERNAL HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _make_key(prefix: str, relative: str) -> str:
    """Build s3 key: 'tradingagent/data/stocks/2016/RELIANCE.parquet'"""
    return f"{prefix}/{relative}" if prefix else relative


def _collect_local_files(prefix: str) -> list[tuple[Path, str]]:
    """Return [(local_path, s3_key)] for all parquet files under stocks/ and index/."""
    results = []
    for base_dir in [STOCKS_DIR, INDEX_DIR]:
        for f in sorted(base_dir.rglob("*.parquet")):
            relative = f.relative_to(DATA_DIR.parent).as_posix()   # "data/stocks/..."
            key = _make_key(prefix, relative)
            results.append((f, key))
    return results


def _list_s3_objects(s3, bucket: str, prefix: str) -> list[tuple[str, int]]:
    """Return [(key, size)] for all parquet objects under prefix/data/ in the bucket."""
    s3_prefix = f"{prefix}/data/" if prefix else "data/"
    results = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=s3_prefix):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(".parquet"):
                results.append((obj["Key"], obj["Size"]))
    return results


def _list_s3_sizes(s3, bucket: str, prefix: str) -> dict[str, int]:
    return {key: size for key, size in _list_s3_objects(s3, bucket, prefix)}


def _upload_one(s3, bucket: str, local: Path, key: str) -> None:
    s3.upload_file(str(local), bucket, key)


def _download_one(s3, bucket: str, key: str, local: Path) -> None:
    local.parent.mkdir(parents=True, exist_ok=True)
    s3.download_file(bucket, key, str(local))


def _run_parallel(items: list, worker, desc: str) -> None:
    if not items:
        return
    errors = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(worker, item): item for item in items}
        with tqdm(total=len(futures), desc=desc, unit="file") as pbar:
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    errors.append(f"{futures[future]}: {e}")
                finally:
                    pbar.update(1)
    if errors:
        log.error(f"{len(errors)} transfer(s) failed:")
        for err in errors[:10]:
            log.error(f"  {err}")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sync trading data with S3")
    parser.add_argument("command", choices=["upload", "download", "status"])
    parser.add_argument("--bucket", default=S3_BUCKET, help=f"S3 bucket name (default: {S3_BUCKET})")
    parser.add_argument("--prefix", default=S3_PREFIX, help=f"Folder prefix inside bucket (default: {S3_PREFIX})")
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    if args.command == "upload":
        upload(args.bucket, args.prefix)
    elif args.command == "download":
        download(args.bucket, args.prefix)
    elif args.command == "status":
        status(args.bucket, args.prefix)
