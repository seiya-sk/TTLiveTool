#!/bin/bash
# ジョブを Healthchecks.io の /start … /<exit-code> で挟む。
# UUID 未設定でも、ジョブ本体は必ず実行される(監視の不備でジョブを
# 止めないため)。curl の失敗も無視する。
set -o pipefail
HC_BASE="https://hc-ping.com"
if [ -n "$HEALTHCHECKS_CLEANUP_UUID" ]; then
    curl -fsS -m 10 "$HC_BASE/$HEALTHCHECKS_CLEANUP_UUID/start" -o /dev/null || true
fi
"$@"
rc=$?
if [ -n "$HEALTHCHECKS_CLEANUP_UUID" ]; then
    curl -fsS -m 10 "$HC_BASE/$HEALTHCHECKS_CLEANUP_UUID/$rc" -o /dev/null || true
fi
exit $rc
