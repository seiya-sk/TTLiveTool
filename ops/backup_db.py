#!/usr/bin/env python3
"""録画DBの2階建てバックアップ。

  短期(hourly): 数時間ごと。直近の事故(誤操作・不正なマイグレーション)から
                 素早く戻すためのもの。世代を多めに、保持は短く。
  長期(daily) : 1日1本。短期が全部流れた後でも、その日の状態に戻れるようにする。

稼働中DBに対して sqlite3 の backup API を使う(ファイルコピーではない)。
録画プロセスが書き込んでいる最中でも一貫したスナップショットが取れ、
録画を止める必要がない。

バックアップからは **生ペイロード(live_event_raw_payloads)を除外する**。
DB全体 2.3GB のうち大半がこれで、しかも稼働中DBでも1日で消える一時データ
(cleanup_job.py が保持1日で削除している)。バックアップの目的は
「集計済みのイベント・セッション・設定を失わないこと」なので、翌日には
消える生バイト列を何世代も抱える意味がない。生ペイロードごと残したい
場合は --keep-raw-payloads を付ける。

世代数はディスクを見て決める。既定は短期4世代 + 長期3世代。
--max-total-gb を超えそうなら古い短期から先に落とす。
"""
import argparse
import contextlib
import errno
import glob
import logging
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("backup_db")
JST = timezone(timedelta(hours=9))


# --- 二重起動の防止(2026-09-03)-------------------------------------------
# DBが7.2GBに育ち、バックアップに12分かかるようになった。以前(74秒)なら
# 現実的に起きなかった「タイマーの定期実行と手動実行が重なる」が起こりうる。
#
# 実際に発生したときの害はディスクとI/O。一時ファイルが倍(24GB)になり
# ディスク使用率が57%まで上がり、両者がI/Oを奪い合って両方遅くなった。
# **データ破損は起きない** -- それぞれ別ファイルに書き、稼働中DBは
# 読み取り専用で開いているため。
#
# 既に実行中なら **正常終了する**(エラーにしない)。タイマーから起動された
# 場合に失敗扱いにすると、systemd の失敗通知が鳴り続けることになる。
# 実行中のバックアップがあるなら目的は果たされている。
LOCK_PATH_SUFFIX = ".backup.lock"
# 異常終了でロックが残った場合の保険。PIDの生存を先に見るので通常は使わないが、
# PIDが再利用されている場合に備えて時間でも切る。
LOCK_STALE_SEC = 3600


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM     # 権限が無いだけなら生きている
    return True


@contextlib.contextmanager
def single_instance(lock_path: str):
    """二重起動を弾く。取れたら True、既に走っていたら False を yield する。

    古いロックの扱いは2段構え。まずPIDの生存を見て、死んでいれば奪う。
    PIDが再利用されている可能性に備えて、時刻でも切る。
    """
    holder = None
    try:
        with open(lock_path, encoding="utf-8") as f:
            holder = int(f.read().strip() or 0)
    except (OSError, ValueError):
        holder = None

    if holder:
        alive = _pid_alive(holder)
        try:
            age = time.time() - os.path.getmtime(lock_path)
        except OSError:
            age = 0.0
        if alive and age < LOCK_STALE_SEC:
            yield False
            return
        logger.warning(
            "古いロックを無視します(pid=%s %s / %.0f分前)",
            holder, "生存" if alive else "不在", age / 60,
        )

    try:
        with open(lock_path, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError as exc:
        # ロックが作れないのを理由にバックアップを止めない。二重起動の
        # 防止は利便性のためで、バックアップそのものより優先しない。
        logger.warning("ロックファイルを作成できませんでした(続行します): %s", exc)
        yield True
        return

    try:
        yield True
    finally:
        try:
            os.remove(lock_path)
        except OSError:
            pass


RAW_PAYLOAD_TABLE = "live_event_raw_payloads"


def _cleanup(*paths: str) -> None:
    for path in paths:
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                os.remove(path + suffix)
            except OSError:
                pass


def slim(tmp: str, slim_path: str) -> tuple[float, float, int]:
    """コピー済みのバックアップから生ペイロードを落として詰め直す。

    **稼働中DBには一切触らない**(触るのは自分が作ったコピーだけ)。
    DELETE しただけではファイルは縮まない(auto_vacuum=0 なので解放された
    ページが空きページとして残るだけ)ので、VACUUM で詰める。

    VACUUM ではなく VACUUM INTO を使う理由: in-place の VACUUM は一時ファイルを
    SQLite の一時ディレクトリ(TMPDIR 依存)に作る。バックアップ先とは別の
    パーティションかもしれず、そこの空き容量は事前チェックの対象外になる。
    VACUUM INTO なら出力先を明示できるので、見積もったディスクの中で完結する。

    戻り値: (削除前GB, 削除後GB, 削除件数)
    """
    before_gb = os.path.getsize(tmp) / 1024 ** 3
    conn = sqlite3.connect(tmp)
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (RAW_PAYLOAD_TABLE,)
        ).fetchone()
        if not exists:
            logger.info("%s が無いバックアップ(スキーマが古い?)。そのまま保存する", RAW_PAYLOAD_TABLE)
            return before_gb, before_gb, 0
        deleted = conn.execute(f"DELETE FROM {RAW_PAYLOAD_TABLE}").rowcount
        conn.commit()
        _cleanup(slim_path)
        conn.execute("VACUUM INTO ?", (slim_path,))
    finally:
        conn.close()
    return before_gb, os.path.getsize(slim_path) / 1024 ** 3, deleted


def take_backup(db_path: str, dest: str, keep_raw_payloads: bool = False) -> float:
    """稼働中でも安全なスナップショットを取る。一時ファイルに書いてから
    rename する -- 途中で落ちても中途半端なファイルが残らない。"""
    tmp = dest + ".partial"
    slim_path = dest + ".slim"
    _cleanup(tmp, slim_path)
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()

    if keep_raw_payloads:
        os.replace(tmp, dest)
        _cleanup(tmp)
        return os.path.getsize(dest) / 1024 ** 3

    try:
        before_gb, after_gb, deleted = slim(tmp, slim_path)
    except Exception:
        # 軽量化に失敗しても、取れているコピーは捨てない。バックアップが
        # 存在しないより、大きいバックアップが存在するほうが良い。
        logger.exception("生ペイロードの除外に失敗した。フルコピーのまま保存する")
        os.replace(tmp, dest)
        _cleanup(tmp, slim_path)
        return os.path.getsize(dest) / 1024 ** 3

    if deleted:
        logger.info("生ペイロード %s 件を除外: %.2f GB -> %.2f GB (%.0f%% 削減)",
                    f"{deleted:,}", before_gb, after_gb,
                    (1 - after_gb / before_gb) * 100 if before_gb else 0)
    os.replace(slim_path, dest)
    _cleanup(tmp, slim_path)
    return os.path.getsize(dest) / 1024 ** 3


def prune(pattern: str, keep: int) -> list[str]:
    """古い世代から削除。新しい keep 本を残す。"""
    files = sorted(glob.glob(pattern))
    removed = []
    for path in files[:-keep] if keep > 0 else files:
        try:
            os.remove(path)
            removed.append(os.path.basename(path))
        except OSError:
            pass
    return removed


def total_gb(directory: str) -> float:
    return sum(os.path.getsize(p) for p in glob.glob(os.path.join(directory, "*.db"))) / 1024 ** 3


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db-path", required=True)
    p.add_argument("--dest-dir", default="data/backups")
    p.add_argument("--tier", choices=("hourly", "daily"), required=True)
    p.add_argument("--keep-hourly", type=int, default=4)
    p.add_argument("--keep-daily", type=int, default=3)
    p.add_argument("--max-total-gb", type=float, default=20.0,
                   help="バックアップ合計がこれを超えたら古い短期から落とす")
    p.add_argument("--min-free-gb", type=float, default=10.0,
                   help="この空きを下回る見込みならバックアップを中止する")
    p.add_argument("--keep-raw-payloads", action="store_true",
                   help="生ペイロードも含めたフルコピーを取る(既定は除外して軽量化)")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    os.makedirs(args.dest_dir, exist_ok=True)

    lock_path = os.path.join(args.dest_dir, "." + os.path.basename(args.db_path) + LOCK_PATH_SUFFIX)
    with single_instance(lock_path) as acquired:
        if not acquired:
            logger.info("別のバックアップが実行中のためスキップします。")
            return 0
        return _run_backup(args)


def _run_backup(args) -> int:
    db_gb = os.path.getsize(args.db_path) / 1024 ** 3
    free_gb = shutil.disk_usage(args.dest_dir).free / 1024 ** 3

    # 空きを食い潰してから気づくのでは遅い。取る前に見積もる。
    # 軽量化する場合は、コピー(db_gb)と VACUUM INTO の出力が一時的に同居する
    # ので、ピークは最大で db_gb の2倍になる。そのピークで見積もる。
    peak_gb = db_gb if args.keep_raw_payloads else db_gb * 2
    if free_gb - peak_gb < args.min_free_gb:
        logger.error(
            "バックアップを中止: ピーク %.1fGB を使うと空きが %.1fGB になり、下限 %.1fGB を割る",
            peak_gb, free_gb - peak_gb, args.min_free_gb,
        )
        return 1

    stamp = datetime.now(JST).strftime("%Y%m%d_%H%M")
    dest = os.path.join(args.dest_dir, f"proxy5_{args.tier}_{stamp}.db")
    t0 = time.time()
    size = take_backup(args.db_path, dest, keep_raw_payloads=args.keep_raw_payloads)
    logger.info("%s バックアップ: %s (%.2f GB, 元DB %.2f GB, %.1f秒)",
                args.tier, os.path.basename(dest), size, db_gb, time.time() - t0)

    keep = args.keep_hourly if args.tier == "hourly" else args.keep_daily
    removed = prune(os.path.join(args.dest_dir, f"proxy5_{args.tier}_*.db"), keep)
    if removed:
        logger.info("古い%s世代を削除: %s", args.tier, ", ".join(removed))

    # 合計が上限を超えたら、長期より先に短期を削る
    while total_gb(args.dest_dir) > args.max_total_gb:
        hourly = sorted(glob.glob(os.path.join(args.dest_dir, "proxy5_hourly_*.db")))
        if len(hourly) <= 1:
            logger.warning("合計 %.1fGB が上限 %.1fGB を超えているが、"
                           "これ以上短期世代を削れない", total_gb(args.dest_dir), args.max_total_gb)
            break
        os.remove(hourly[0])
        logger.info("合計容量の上限超過のため削除: %s", os.path.basename(hourly[0]))

    print(f"\n  保管中のバックアップ (合計 {total_gb(args.dest_dir):.1f} GB / ディスク空き "
          f"{shutil.disk_usage(args.dest_dir).free / 1024 ** 3:.0f} GB)")
    for path in sorted(glob.glob(os.path.join(args.dest_dir, "*.db"))):
        print(f"    {os.path.basename(path):40} {os.path.getsize(path)/1024**3:.2f} GB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
