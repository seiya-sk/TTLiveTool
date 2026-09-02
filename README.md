# TTSLiveTool

TikTokライブのコメント/ギフト/視聴者数などのイベントを受信し、SQLiteに保存する(`tiktok_monitor`)。事後振り返り用のWebダッシュボード(`dashboard`)から過去配信を一覧・参照できる。設計書は `docs/tiktok-live-analytics-design-v2.md` を参照。

## データ取得(tiktok_monitor)

### セットアップ

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

### テスト

```
pytest tests/
```

### 実行

```
python -m tiktok_monitor.main <TikTokユーザー名>
```

- `--db-path` : SQLiteファイルの保存先(既定: `data/tts_live_tool.db`)
- `--idle-timeout` : 無イベント何秒で配信終了とみなすか(既定: 60)
- `--verbose` : 受信イベントごとのデバッグログを表示
- 実行中に Ctrl+C を押すと手動終了(`end_detection_type='manual'`)として記録される
- プロジェクトルート(このREADMEがあるディレクトリ)から実行すること
- 配信開始直後、ヘッドレスブラウザ(Playwright)で配信画面を匿名キャプチャし1枚保存する(`data/screenshots/`、`TTS_SCREENSHOT_DIR`で変更可)。取得に失敗しても収集自体は継続する
- 配信開始直後、まだアイコン未取得のライバーであればTikTokのアバター画像も取得しキャッシュする(`data/avatars/`、`TTS_AVATAR_DIR`で変更可)。取得に失敗しても収集自体は継続する。既存ライバー分をまとめて取得/更新したい場合は `python -m tiktok_monitor.fetch_avatars` を実行する(アカウントIDを引数に渡すと対象を絞り込める)

### DB確認例

```
sqlite3 data/tts_live_tool.db "select event_type, count(*) from live_events group by 1;"
```

## ダッシュボード(dashboard)

Next.jsで実装した事後振り返り用Webダッシュボード。`tiktok_monitor`が書き込んだSQLiteを読み取り専用で参照する。

```
cd dashboard
cp .env.local.example .env.local   # 必要ならTTS_DB_PATHを調整(既定: ../data/tts_live_tool.db)
npm install
npm run dev
```

`http://localhost:3000` を開くとライブ一覧が表示され、各行から配信ごとの詳細(時系列グラフ・バトル相手・ギフトランキング/明細・入室管理・フォロー・コメント)を確認できる。
