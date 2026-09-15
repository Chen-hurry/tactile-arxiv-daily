#!/usr/bin/env bash
# 安装定时任务：每天 09:15 / 21:15（本机时区）运行 scripts/update.sh（有 GitHub 远程时同步云端数据，否则本机拉取）。
# arXiv 每个工作日约美东 20:00 发布新论文。卸载：bash scripts/install_cron.sh --remove
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
TAG="# tactile-arxiv-daily"
LINE="15 9,21 * * * bash $DIR/scripts/update.sh $TAG"
current="$(crontab -l 2>/dev/null | grep -v "$TAG" || true)"
if [[ "${1:-}" == "--remove" ]]; then
  printf '%s\n' "$current" | sed '/^$/d' | crontab -
  echo "已移除定时任务"
else
  printf '%s\n%s\n' "$current" "$LINE" | sed '/^$/d' | crontab -
  echo "已安装定时任务："; crontab -l | grep "$TAG"
fi
