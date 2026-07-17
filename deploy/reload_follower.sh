#!/usr/bin/env bash
# 逐一滾動重啟 follower（拉版後）。單一失敗不中止其餘，最後回報。
# 需求：執行者對 `systemctl restart filet-follower@*` 有 sudo NOPASSWD（見部署文件）。
set -uo pipefail
units=$(systemctl list-units 'filet-follower@*' --no-legend --plain | awk '{print $1}')
fail=0
for u in $units; do
  echo "restarting $u ..."
  sudo systemctl restart "$u" || { echo "  FAILED: $u"; fail=1; }
  sleep 3
done
exit $fail
