import traceback
import time
import re
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials

from TikTokLive import TikTokLiveClient
from TikTokLive.events import (
    GiftEvent, FollowEvent, LikeEvent, ConnectEvent, 
    DisconnectEvent, JoinEvent, CommentEvent, BarrageEvent
)
from TikTokLive.client.errors import UserOfflineError

# ==========================================
# 設定情報の入力
# ==========================================
TIKTOK_USERNAME = "ya_desu_05"
SPREADSHEET_ID = "1JDQOULrilKELxRP4yCOew1bS6NbcUWlkjchWLIQSgjo"

# ==========================================
# Googleスプレッドシートの初期設定
# ==========================================
SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive'
]
try:
    credentials = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
    gc = gspread.authorize(credentials)
    sh = gc.open_by_key(SPREADSHEET_ID)
except Exception as e:
    print(f"❌ スプレッドシート認証エラー: {e}")
    exit()

def get_or_create_worksheet(sheet_title, headers):
    try:
        ws = sh.worksheet(sheet_title)
        if not ws.get_all_values():
            ws.append_row(headers)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=sheet_title, rows="1000", cols="20")
        ws.append_row(headers)
    return ws

# シートの定義（all_battlesの列を拡張しました）
ws_summary = get_or_create_worksheet("all_summaries", ["セッションID", "配信開始", "配信終了", "総配信時間", "最終合計いいね数"])
ws_gifts = get_or_create_worksheet("all_gifts", ["セッションID", "時間", "ユーザー名", "ギフレベ", "ギフト名", "コイン単価", "ダイヤ単価"])
ws_follows = get_or_create_worksheet("all_follows", ["セッションID", "時間", "ユーザー名", "ギフレベ"])
ws_joins = get_or_create_worksheet("all_joins", ["セッションID", "時間", "ユーザー名", "ギフレベ", "入室回数/状態"])
ws_comments = get_or_create_worksheet("all_comments", ["セッションID", "時間", "ユーザー名", "ギフレベ", "メンバーレベル", "コメント内容"])
ws_battles = get_or_create_worksheet("all_battles", ["セッションID", "時間", "バトル回数", "自分の順位", "相手のID", "相手の順位"])

# ==========================================
# グローバル変数
# ==========================================
session_id = None
start_time = None
total_likes = 0
offline_time = None
GRACE_PERIOD_SECONDS = 300  # 5分

comment_batch = []
last_comment_flush_time = time.time()
COMMENT_FLUSH_INTERVAL = 180  

user_join_counts = {}
join_batch = []
last_join_flush_time = time.time()
JOIN_FLUSH_INTERVAL = 60  

# バトル管理用
battle_count = 0
recorded_battle_ids = set()

# ==========================================
# ツール関数
# ==========================================
def log_error(event_name, error):
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    error_msg = f"[{current_time}] ⚠️ {event_name} でエラー: {error}\n{traceback.format_exc()}\n"
    print(f"[{current_time}] ⚠️ エラー発生（error_log.txt を確認）: {error}")
    with open("error_log.txt", "a", encoding="utf-8") as f:
        f.write(error_msg)

def write_to_sheet(worksheet, row_data):
    try:
        worksheet.append_row(row_data)
    except Exception as e:
        log_error(f"スプレッドシート書き込み ({worksheet.title})", e)

def get_safe_g_level(user):
    try:
        lvl = getattr(user, 'gifter_level', None)
        if lvl is None and hasattr(user, 'pay_grade'):
            lvl = getattr(user.pay_grade, 'level', None)
        return int(lvl) if lvl is not None else 0
    except Exception:
        return 0

def flush_comments():
    global comment_batch, last_comment_flush_time
    if not comment_batch: return
    try:
        batch_to_write = list(comment_batch) 
        comment_batch.clear()
        ws_comments.append_rows(batch_to_write, value_input_option='USER_ENTERED')
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 📝 コメント {len(batch_to_write)} 件を一括保存しました。")
        last_comment_flush_time = time.time()
    except Exception as e: log_error("コメント一括保存", e)

def flush_joins():
    global join_batch, last_join_flush_time
    if not join_batch: return
    try:
        batch_to_write = list(join_batch) 
        join_batch.clear()
        ws_joins.append_rows(batch_to_write, value_input_option='USER_ENTERED')
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 🚪 入室/VIP記録 {len(batch_to_write)} 件を一括保存しました。")
        last_join_flush_time = time.time()
    except Exception as e: log_error("入室一括保存", e)

def finish_live_session():
    global session_id, start_time, total_likes
    if not session_id: return 

    flush_comments()
    flush_joins()

    end_time = datetime.now()
    duration = end_time - start_time if start_time else "不明"
    end_time_str = end_time.strftime('%Y-%m-%d %H:%M:%S')
    start_time_str = start_time.strftime('%Y-%m-%d %H:%M:%S') if start_time else "不明"
    
    print(f"\n[{end_time.strftime('%H:%M:%S')}] 🛑 配信終了。サマリーを保存します。")
    write_to_sheet(ws_summary, [session_id, start_time_str, end_time_str, str(duration), total_likes])
    session_id = None

# ==========================================
# クライアント初期化 ＆ バトル（順位・回数）横取り処理
# ==========================================
client = TikTokLiveClient(unique_id=TIKTOK_USERNAME)
original_emit = client.emit

def custom_emit(event_name, *args, **kwargs):
    global recorded_battle_ids, session_id, battle_count
    
    event_str = str(event_name)
    event_obj = args[0] if args else None

    if any(keyword in event_str for keyword in ["Link", "Battle", "Armies"]):
        try:
            raw_str = str(event_obj)
            
            # バトルの固有ID（背番号）を取得
            b_id_match = re.search(r"battle_id[=:]\s*['\"]?(\d+)", raw_str)
            b_id = b_id_match.group(1) if b_id_match else None

            # status=3 または action=5 が「バトル終了・決着」の合図
            is_end = "status=3" in raw_str or "action=5" in raw_str or "PunishFinish" in event_str

            if b_id and is_end and b_id not in recorded_battle_ids:
                # ユーザーIDと表示名の紐付けを取得
                user_map = {}
                for match in re.finditer(r"user_id=(\d+).*?display_id=['\"]([^'\"]+)['\"]", raw_str):
                    uid, did = match.groups()
                    user_map[uid] = did

                # ユーザーIDと順位の紐付けを取得
                rank_map = {}
                for match in re.finditer(r"anchor_id_str=['\"](\d+)['\"].*?host_rank=(\d+)", raw_str):
                    uid, rank = match.groups()
                    rank_map[uid] = int(rank)

                if user_map and rank_map:
                    recorded_battle_ids.add(b_id)
                    battle_count += 1
                    
                    # 自分のユーザーIDと順位を特定
                    my_uid = next((uid for uid, did in user_map.items() if TIKTOK_USERNAME in did), None)
                    my_rank = rank_map.get(my_uid, "不明") if my_uid else "不明"

                    current_time = datetime.now().strftime("%H:%M:%S")
                    safe_session = session_id if session_id else "unknown_session"

                    # 相手（最大3人）のデータを1人ずつスプレッドシートに記録
                    for uid, did in user_map.items():
                        if uid != my_uid and did != "0" and did != "None":
                            opp_rank = rank_map.get(uid, "不明")
                            write_to_sheet(ws_battles, [safe_session, current_time, battle_count, my_rank, did, opp_rank])
                            print(f"[{current_time}] ⚔️ バトル終了(第{battle_count}回): 相手 {did} (順位: {opp_rank}) | 自分順位: {my_rank}")

        except Exception:
            pass
            
    return original_emit(event_name, *args, **kwargs)

client.emit = custom_emit

# ==========================================
# イベントごとの処理
# ==========================================
@client.on(ConnectEvent)
async def on_connect(event: ConnectEvent):
    global start_time, session_id, total_likes, offline_time, user_join_counts, recorded_battle_ids, battle_count
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    if offline_time and (datetime.now() - offline_time).total_seconds() < GRACE_PERIOD_SECONDS:
        print(f"[{current_time}] 🔄 再接続に成功しました！集計を継続します。")
    else:
        start_time = datetime.now()
        total_likes = 0
        battle_count = 0
        user_join_counts.clear()
        join_batch.clear()
        comment_batch.clear()
        recorded_battle_ids.clear() 
        session_id = start_time.strftime("%Y%m%d_%H%M%S")
        print(f"\n[{current_time}] 🚀 新しいライブ配信を検知しました！ (Session: {session_id})")
        
    offline_time = None

@client.on(JoinEvent)
async def on_join(event: JoinEvent):
    global user_join_counts, join_batch, last_join_flush_time
    try:
        g_level = get_safe_g_level(event.user)
        if g_level >= 0: # 💡必要に応じて 25 に戻してください
            current_time = datetime.now().strftime("%H:%M:%S")
            name = event.user.nickname
            user_id = getattr(event.user, 'user_id', getattr(event.user, 'id', getattr(event.user, 'unique_id', name)))
            
            user_join_counts[user_id] = user_join_counts.get(user_id, 0) + 1
            join_count = user_join_counts[user_id]
            safe_session = session_id if session_id else "unknown_session"
            
            join_batch.append([safe_session, current_time, name, g_level, join_count])
            print(f"[{current_time}] 🚪 {name}(Lv.{g_level}) が入室！(累計 {join_count}回目)")
            
            if time.time() - last_join_flush_time >= JOIN_FLUSH_INTERVAL:
                flush_joins()
    except Exception as e:
        pass

@client.on(BarrageEvent)
async def on_barrage(event: BarrageEvent):
    """VIP（高レベル）ユーザーの派手な入室エフェクトをキャッチして入室記録に合流させる"""
    global user_join_counts, join_batch, last_join_flush_time
    try:
        if not hasattr(event, 'user') or event.user is None:
            return
        g_level = get_safe_g_level(event.user)
        if g_level >= 25: 
            current_time = datetime.now().strftime("%H:%M:%S")
            name = event.user.nickname
            user_id = getattr(event.user, 'user_id', getattr(event.user, 'id', getattr(event.user, 'unique_id', name)))
            
            user_join_counts[user_id] = user_join_counts.get(user_id, 0) + 1
            join_count = user_join_counts[user_id]
            safe_session = session_id if session_id else "unknown_session"
            
            # 入室記録に「VIP登場」というステータスをつけて保存
            join_batch.append([safe_session, current_time, name, g_level, f"VIP登場({join_count})"])
            print(f"[{current_time}] 🏎️💨 VIP検知: {name}(Lv.{g_level}) がエフェクト付きで入室！")
            
            if time.time() - last_join_flush_time >= JOIN_FLUSH_INTERVAL:
                flush_joins()
    except Exception as e:
        pass

@client.on(GiftEvent)
async def on_gift(event: GiftEvent):
    try:
        current_time = datetime.now().strftime("%H:%M:%S")
        name = event.user.nickname
        gift_name = event.gift.name
        g_level = get_safe_g_level(event.user)
        
        if hasattr(event.gift, 'info') and event.gift.info is not None:
            coin_count = getattr(event.gift.info, 'coin_count', 0) or 0
            diamond_count = getattr(event.gift.info, 'diamond_count', getattr(event.gift.info, 'diamond', 0)) or 0
        else:
            coin_count = getattr(event.gift, 'coin_count', 0) or 0
            diamond_count = getattr(event.gift, 'diamond_count', getattr(event.gift, 'diamond', 0)) or 0
        
        safe_session = session_id if session_id else "unknown_session"
        write_to_sheet(ws_gifts, [safe_session, current_time, name, g_level, gift_name, coin_count, diamond_count])
        print(f"[{current_time}] 🎁 {name}(Lv.{g_level}) が {gift_name} を送信！")
    except Exception as e:
        pass

@client.on(CommentEvent)
async def on_comment(event: CommentEvent):
    global comment_batch, last_comment_flush_time
    try:
        current_time = datetime.now().strftime("%H:%M:%S")
        name = event.user.nickname
        comment = event.comment
        g_level = get_safe_g_level(event.user)
        
        badge_lvl = getattr(event.user, 'badge_level', None)
        member_lvl = getattr(event.user, 'member_level', None)
        member_level = badge_lvl if badge_lvl is not None else (member_lvl if member_lvl is not None else 0)
        
        safe_session = session_id if session_id else "unknown_session"
        comment_batch.append([safe_session, current_time, name, g_level, member_level, comment])
        
        if time.time() - last_comment_flush_time >= COMMENT_FLUSH_INTERVAL:
            flush_comments()
    except Exception as e:
        pass

@client.on(FollowEvent)
async def on_follow(event: FollowEvent):
    try:
        current_time = datetime.now().strftime("%H:%M:%S")
        name = event.user.nickname
        g_level = get_safe_g_level(event.user)
        safe_session = session_id if session_id else "unknown_session"
        write_to_sheet(ws_follows, [safe_session, current_time, name, g_level])
        print(f"[{current_time}] 👤 {name}(Lv.{g_level}) がフォローしました！")
    except Exception as e:
        pass

@client.on(LikeEvent)
async def on_like(event: LikeEvent):
    global total_likes
    try:
        if hasattr(event, 'total'):
            total_likes = event.total
        else:
            total_likes += getattr(event, 'count', 1)
        current_time = datetime.now().strftime("%H:%M:%S")
        print(f"[{current_time}] ❤️ いいね更新: 現在 {total_likes} 回")
    except Exception as e:
        pass

# ==========================================
# メイン処理
# ==========================================
if __name__ == "__main__":
    print(f"👁️ [{TIKTOK_USERNAME}] の配信監視をスタートします... (Ctrl+C で安全に停止)")
    
    while True:
        try:
            client.run()
        except UserOfflineError:
            pass
        except KeyboardInterrupt:
            print("\n🛑 監視ツールを手動で停止しました。残りのデータを保存します...")
            if session_id:
                finish_live_session()
            break
        except Exception as e:
            log_error("メインループ", e)

        if offline_time is None:
            offline_time = datetime.now()

        offline_duration = (datetime.now() - offline_time).total_seconds()

        if offline_duration >= GRACE_PERIOD_SECONDS:
            if session_id:
                finish_live_session()
            current_time = datetime.now().strftime("%H:%M:%S")
            print(f"[{current_time}] 💤 まだオフラインです。60秒後に再確認します...")
            time.sleep(60)
        else:
            remain = int(GRACE_PERIOD_SECONDS - offline_duration)
            current_time = datetime.now().strftime("%H:%M:%S")
            print(f"[{current_time}] ⚠️ 通信切断。完全終了判定まで残り {remain} 秒... (再接続試行)")
            time.sleep(10)