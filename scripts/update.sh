#!/usr/bin/env bash
# 供 cron 调用：加锁防重入，日志写入 logs/
#   - 默认（仓库有 GitHub 远程时）：只 git pull，数据由 GitHub Actions 在云端更新，避免两边各自更新产生冲突
#   - TACTILE_LOCAL_FETCH=1 或没有远程仓库：在本机直接运行拉取（可带参数，如 --backfill）
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs
PY="${TACTILE_PYTHON:-python3}"
exec 9>logs/.lock
flock -n 9 || { echo "another update is running"; exit 0; }
{
  echo "===== $(date '+%F %T') ====="
  if [[ "${TACTILE_LOCAL_FETCH:-0}" != "1" ]] && git remote get-url origin >/dev/null 2>&1; then
    git pull --ff-only
  else
    "$PY" tactile_arxiv.py "$@"
  fi
} >> "logs/update-$(date +%Y%m).log" 2>&1
