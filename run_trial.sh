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
# 起動元シェルに TTS_DISABLE_SCREENSHOTS が残っていると、スクショが
# 黙って無効になる。**「設定しない」だけでは足りず、明示的に消す必要がある**
# -- 2026-09-02 の棚卸しで、01:37の再起動から約10時間、セッション13本が
# 1枚も撮れていなかった。ログには1行も出ない(撮影を試みないので失敗も
# しない)ため、DBのスクショ件数を数えるまで気づけなかった。
unset TTS_DISABLE_SCREENSHOTS

# 上記の変数を run_trial.sh 側で設定するのはやめた(2026-09-01)。Linux移行時の保険だったが、
# playwright install --with-deps chromium を導入し、screenshot.py の
# <video> 待ち時間バグ(6秒固定では VPS+プロキシ経由で間に合わなかった)を
# 直したうえで実撮影を確認済み。撮影は配信開始10分後に1枚
# (config.py の screenshot_delay_sec)。
# --pool-file は指定しない。監視対象は streamers テーブル
# (archived=0 AND enabled=1)から読み、巡回1周ごとに読み直す。
# ダッシュボードで「無効にする」を押した内容が次の巡回から効く。
#
# 緊急時にファイル固定へ戻すには、下に --pool-file sample/streamers_150.txt を
# 足す(指定があればDBより優先される)。比較用にファイルは残してある。
exec python -m tiktok_monitor.proxy_pool_trial \
  --proxies-file data/proxy_pool_trial/proxy5_ips.txt \
  --check-pace-sec 5.0 \
  --db-path data/proxy_pool_trial/proxy5.db \
  --events-path data/proxy_pool_trial/proxy5_events.jsonl \
  --verbose 2>&1 | tee -a data/proxy_pool_trial/trial.log
