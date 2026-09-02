# 運用ユニットの控え

`/etc/systemd/system/` と `/etc/nginx/` に配置してある実ファイルの複製。
VPS が失われるとこれらも一緒に消えるので、コードと一緒に版管理する。

**ここにあるのは複製であって、動いている実体ではない。** 変更したら
配置し直して `systemctl daemon-reload` するまで反映されない。

    cp ops/systemd/tts-*.service ops/systemd/tts-*.timer /etc/systemd/system/
    systemctl daemon-reload

## 意図的に含めていないもの

- `/etc/tts-notify.env` -- Chatwork トークン、Healthchecks UUID、
  Euler Stream の APIキー。**認証情報なので版管理しない**(chmod 600)。
  各ユニットは `EnvironmentFile=-/etc/tts-notify.env` で参照するだけ。
- `/etc/nginx/.htpasswd` -- ダッシュボードの Basic 認証。同上。

復旧時はこの2つだけ手で作り直す。
