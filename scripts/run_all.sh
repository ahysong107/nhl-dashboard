#!/bin/zsh
# Daily Ice Edge refresh: ./scripts/run_all.sh [YYYY-MM-DD]
set -e
cd "$(dirname "$0")/.."
DATE="${1:-$(date +%Y-%m-%d)}"
python3 scripts/fetch_data.py "$DATE"
python3 scripts/build_site_data.py "$DATE"
