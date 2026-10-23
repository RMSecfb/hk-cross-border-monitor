#!/usr/bin/env bash
# 把 data/ 與 config/ 的變更 commit 回 repo；遇到推送衝突自動重試
set -e
git config user.name  "hk-monitor-bot"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add -A data config
if git diff --cached --quiet; then
  echo "沒有新資料"; exit 0
fi
git commit -m "$1 $(TZ=Asia/Hong_Kong date +'%F %H:%M')"
for i in 1 2 3 4 5; do
  git pull --rebase -X theirs && git push && exit 0
  sleep $((i * 5))
done
echo "推送失敗"; exit 1
