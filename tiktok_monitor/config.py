import os
from dataclasses import dataclass

from . import proxy as proxy_module


@dataclass(frozen=True)
class Settings:
    username: str
    db_path: str = "data/tts_live_tool.db"
    # 「無イベントが続いたら生存を再確認する」までの秒数。**これ自体は
    # セッションを終了させない** -- 終了は LiveEndEvent / NORMAL_CLOSURE、
    # または別IPでの is_live 再確認が偽だったときだけ(2026-09-01の設計変更)。
    # 以前はこの値で自動終了しており、1本の配信が複数セッションに割れる
    # 原因になっていた(perico9108 が2本、haru04150728 が3本に分割)。
    idle_timeout_sec: float = 60.0
    # 配信開始から何秒後にスクショを撮るか。開始直後だと配信の準備画面や
    # 暗転が写りやすいので少し待つ。この時間に達する前に終わった配信は
    # スクショなし(短すぎて分析対象にならないため)。
    screenshot_delay_sec: float = 600.0
    # セッションを再開してよい時間窓(IP乗り換え / 収集プロセス再起動)。
    resume_window_sec: float = 45 * 60
    reconnect_initial_delay_sec: float = 2.0
    reconnect_max_delay_sec: float = 60.0
    screenshot_dir: str = "data/screenshots"
    avatar_dir: str = "data/avatars"
    # Screenshot capture launches a headless Chromium via Playwright
    # (screenshot.py) -- on a fresh Linux host without `playwright install
    # chromium` + its apt dependencies, that launch fails on every single
    # session start. capture_live_screenshot() already treats any failure as
    # non-fatal (recording itself is unaffected), but this lets it be turned
    # off outright rather than repeatedly failing and logging a warning
    # every stream start. True (unset) preserves the original always-on
    # behavior.
    screenshots_enabled: bool = True
    # Optional network proxy (Phase 5 IP-based measurement prep) -- unset by
    # default, meaning every connection goes out on the real IP exactly as
    # before this setting existed. See tiktok_monitor/proxy.py for the
    # supported URL shape and which connections it does/doesn't cover.
    proxy_url: str | None = None

    @classmethod
    def from_args(cls, username: str, db_path: str | None, idle_timeout: float | None) -> "Settings":
        proxy_url = os.environ.get("TTS_PROXY_URL") or None
        if proxy_url:
            # Validated eagerly, at startup, rather than left to surface
            # inside run_with_reconnect's broad retry-on-Exception loop --
            # a typo'd TTS_PROXY_URL is a config mistake to fail fast on,
            # not a transient connection error worth silently retrying.
            proxy_module.parse_proxy_url(proxy_url)
        return cls(
            username=username,
            db_path=db_path or os.environ.get("TTS_DB_PATH", cls.db_path),
            idle_timeout_sec=idle_timeout
            if idle_timeout is not None
            else float(os.environ.get("TTS_IDLE_TIMEOUT_SEC", cls.idle_timeout_sec)),
            screenshot_dir=os.environ.get("TTS_SCREENSHOT_DIR", cls.screenshot_dir),
            avatar_dir=os.environ.get("TTS_AVATAR_DIR", cls.avatar_dir),
            screenshots_enabled=os.environ.get("TTS_DISABLE_SCREENSHOTS", "").strip() not in ("1", "true", "True"),
            proxy_url=proxy_url,
        )
