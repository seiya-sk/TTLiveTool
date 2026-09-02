#!/bin/bash
cd ~/TTSLiveTool
source .venv/bin/activate
# Euler Stream の署名APIキー。設定されていれば TikTokLive が自動的に使う
# (TikTokSigner が os.environ["SIGN_API_KEY"] を読む)。
# /etc/tts-notify.env から **この1つだけ** を取り出す -- 録画プロセスに
# Chatwork のトークンまで渡す必要はないため。未設定なら匿名のままで動く。
if [ -r /etc/tts-notify.env ]; then
  SIGN_API_KEY=$(grep -E '^SIGN_API_KEY=' /etc/tts-notify.env | cut -d= -f2- | tr -d '"'"'"'" ')
  [ -n "$SIGN_API_KEY" ] && export SIGN_API_KEY
fi
# TTS_DISABLE_SCREENSHOTS は外した(2026-09-01)。Linux移行時の保険だったが、
# playwright install --with-deps chromium を導入し、screenshot.py の
# <video> 待ち時間バグ(6秒固定では VPS+プロキシ経由で間に合わなかった)を
# 直したうえで実撮影を確認済み。撮影は配信開始10分後に1枚
# (config.py の screenshot_delay_sec)。
exec python -m tiktok_monitor.proxy_pool_trial \
  --proxies-file data/proxy_pool_trial/proxy5_ips.txt \
  --pool-file sample/streamers_150.txt \
  --check-pace-sec 5.0 \
  --db-path data/proxy_pool_trial/proxy5.db \
  --events-path data/proxy_pool_trial/proxy5_events.jsonl \
  --verbose 2>&1 | tee -a data/proxy_pool_trial/trial.log
