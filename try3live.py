import sys
import traceback
import time
import re
import asyncio
import threading
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials

from TikTokLive import TikTokLiveClient
from TikTokLive.events import (
    GiftEvent, FollowEvent, LikeEvent, ConnectEvent, 
    DisconnectEvent, JoinEvent, CommentEvent, BarrageEvent,
    RoomUserSeqEvent  # 💡 同接取得のために追加
)
from TikTokLive.client.errors import UserOfflineError

# ==========================================
# Windows固有のエラー（WinError 6）を回避する処理
# ==========================================
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# ==========================================
# 👥 監視対象ライバー ＆ スプレッドシートIDの設定
# ==========================================
TARGET_USERS = {
    "nomushirika1": "18B32YY09faxw4Ad_jpO54fqqSkuLn2pGkpPOmHlJRJk",
    "user_torekapanda": "1u1ILTyMcMCORiENo5ErFRT9nzAB3ZowRYgC3TgNXLVk"
}

# ==========================================
# Googleスプレッドシートの共通設定
# ==========================================
SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive'
]
try:
    credentials = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
    gc = gspread.authorize(credentials)
except Exception as e:
    print(f"❌ Googleスプレッドシートの認証に失敗しました: {e}")
    exit()

# グローバルシート変数
ws_summary = None
ws_gifts = None
ws_follows = None
ws_joins = None
ws_comments = None
ws_battles = None

def get_or_create_worksheet(sh, sheet_title, headers):
    try:
        ws = sh.worksheet(sheet_title)
        if not ws.get_all_values():
            ws.append_row(headers)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=sheet_title, rows="1000", cols="20")
        ws.append_row(headers)
    return ws

def setup_sheets_for_current_user(spreadsheet_id):
    global ws_summary, ws_gifts, ws_follows, ws_joins, ws_comments, ws_battles
    try:
        sh = gc.open_by_key(spreadsheet_id)
        # 💡 サマリーに「最高同接」「平均同接」を追加
        ws_summary = get_or_create_worksheet(sh, "all_summaries", ["セッションID", "配信開始", "配信終了", "総配信時間", "最終合計いいね数", "最高同接", "平均同接"])
        ws_gifts = get_or_create_worksheet(sh, "all_gifts", ["セッションID", "時間", "ユーザー名", "ギフレベ", "ファンレベル", "ギフト名", "コイン単価", "ダイヤ単価"])
        ws_follows = get_or_create_worksheet(sh, "all_follows", ["セッションID", "時間", "ユーザー名", "ギフレベ"])
        ws_joins = get_or_create_worksheet(sh, "all_joins", ["セッションID", "時間", "ユーザー名", "ギフレベ", "入室回数/状態"])
        ws_comments = get_or_create_worksheet(sh, "all_comments", ["セッションID", "時間", "ユーザー名", "ギフレベ", "メンバーレベル", "コメント内容"])
        ws_battles = get_or_create_worksheet(sh, "all_battles", ["セッションID", "時間", "相手のID"])
        return True
    except Exception as e:
        print(f"❌ スプレッドシート [{spreadsheet_id}] の読み込みに失敗: {e}")
        return False

# ==========================================
# システム管理用グローバル変数
# ==========================================
session_id = None
start_time = None
total_likes = 0
offline_time = None
GRACE_PERIOD_SECONDS = 300  

# 💡 同接取得用の変数
max_viewers = 0
viewer_counts = []

# フリーズ監視（ウォッチドッグ）用変数
last_event_time = time.time()
current_client = None

# 一括保存用メモリ
comment_batch = []
last_comment_flush_time = time.time()
COMMENT_FLUSH_INTERVAL = 180  

user_join_counts = {}
join_batch = []
last_join_flush_time = time.time()
JOIN_FLUSH_INTERVAL = 60  

recorded_opponents = set()
unknown_event_count = 0
KNOWN_EVENTS = [
    "ConnectEvent", "DisconnectEvent", "JoinEvent", "CommentEvent", 
    "GiftEvent", "FollowEvent", "LikeEvent", "BarrageEvent",
    "RoomUserSeqEvent", "LinkMic", "Battle", "Armies"
]

# ==========================================
# 🛑 フリーズ監視用スレッド (ウォッチドッグ)
# ==========================================
def watchdog():
    global last_event_time, current_client
    while True:
        time.sleep(5)
        if current_client and getattr(current_client, 'connected', False):
            if time.time() - last_event_time > 60:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] ⚠️ 1分以上イベントがありません。強制切断して次の処理へ進みます。")
                try:
                    current_client.stop()
                except Exception:
                    pass
                last_event_time = time.time()

# ==========================================
# 共通ツール関数
# ==========================================
def log_error(event_name, error):
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    error_msg = f"[{current_time}] ⚠️ {event_name} でエラー: {error}\n{traceback.format_exc()}\n"
    print(f"[{current_time}] ⚠️ エラー発生: {error} (error_log.txt を確認してください)")
    with open("error_log.txt", "a", encoding="utf-8") as f:
        f.write(error_msg)

def write_to_sheet(worksheet, row_data):
    try:
        worksheet.append_row(row_data)
    except Exception as e:
        log_error(f"シート書き込み失敗 ({worksheet.title})", e)

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
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 💬 コメント {len(batch_to_write)} 件を一括保存しました。")
        last_comment_flush_time = time.time()
    except Exception as e:
        log_error("コメント一括保存", e)

def flush_joins():
    global join_batch, last_join_flush_time
    if not join_batch: return
    try:
        batch_to_write = list(join_batch)
        join_batch.clear()
        ws_joins.append_rows(batch_to_write, value_input_option='USER_ENTERED')
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 🚪 入室/VIP記録 {len(batch_to_write)} 件を一括保存しました。")
        last_join_flush_time = time.time()
    except Exception as e:
        log_error("入室一括保存", e)

# ==========================================
# 核心ロジック：1人のライバーを監視するメイン関数
# ==========================================
def run_monitor(username, spreadsheet_id):
    global session_id, start_time, total_likes, offline_time
    global comment_batch, join_batch, user_join_counts, recorded_opponents, unknown_event_count
    global current_client, last_event_time
    global max_viewers, viewer_counts  # 💡 同接取得用
    
    print(f"\n──────────────────────────────────────────")
    print(f"🔍 配信状態チェック中... 👉 ユーザー: @{username}")
    print(f"──────────────────────────────────────────")
    
    if not setup_sheets_for_current_user(spreadsheet_id):
        return False 

    # 状態の初期化
    session_id = None
    start_time = None
    total_likes = 0
    offline_time = None
    max_viewers = 0        # 💡 初期化
    viewer_counts.clear()  # 💡 初期化
    comment_batch.clear()
    join_batch.clear()
    user_join_counts.clear()
    recorded_opponents.clear()

    client = TikTokLiveClient(unique_id=username)
    current_client = client
    last_event_time = time.time()
    
    original_emit = client.emit

    def finish_live_session():
        global session_id, start_time, total_likes, max_viewers, viewer_counts
        if not session_id: return 
        flush_comments()
        flush_joins()
        
        end_time = datetime.now()
        duration = end_time - start_time if start_time else "不明"
        
        end_time_str = end_time.strftime('%Y-%m-%d %H:%M:%S')
        start_time_str = start_time.strftime('%Y-%m-%d %H:%M:%S') if start_time else "不明"
        
        # 💡 平均同接の計算
        avg_viewers = round(sum(viewer_counts) / len(viewer_counts)) if viewer_counts else 0
        
        print(f"[{end_time.strftime('%H:%M:%S')}] 🛑 配信終了を確定。サマリーを保存します。")
        # 💡 シートに同接記録を追加して保存
        write_to_sheet(ws_summary, [session_id, start_time_str, end_time_str, str(duration), total_likes, max_viewers, avg_viewers])
        print(f"=== 配信サマリー (Session: {session_id}) ===")
        print(f"総配信時間: {duration}")
        print(f"最終いいね数: {total_likes}")
        print(f"最高同接人数: {max_viewers}")
        print(f"平均同接人数: {avg_viewers}")
        print(f"====================================\n")
        
        session_id = None

    def custom_emit(event_name, *args, **kwargs):
        global recorded_opponents, session_id, unknown_event_count, last_event_time
        
        last_event_time = time.time()
        event_str = str(event_name)
        event_obj = args[0] if args else None

        if any(keyword in event_str for keyword in ["Link", "Battle", "Armies", "Message", "Group"]):
            try:
                event_data = event_obj.as_dict if hasattr(event_obj, "as_dict") else vars(event_obj)
                anchor_info = event_data.get("anchor_info") or getattr(event_obj, "anchor_info", None)
                if anchor_info:
                    raw_data_str = str(anchor_info)
                    pattern = r"display_id['\"]?\s*[:=]\s*['\"]?([a-zA-Z0-9_.-]+)['\"]?"
                    found_ids = re.findall(pattern, raw_data_str)
                    for opponent_id in found_ids:
                        if opponent_id != "0" and opponent_id != "None" and username not in opponent_id:
                            if opponent_id not in recorded_opponents:
                                recorded_opponents.add(opponent_id)
                                current_time = datetime.now().strftime("%H:%M:%S")
                                safe_session = session_id if session_id else "unknown_session"
                                write_to_sheet(ws_battles, [safe_session, current_time, str(opponent_id)])
                                print(f"[{current_time}] ⚔️ バトル相手を検知: {opponent_id}")
            except Exception: pass
            
        if unknown_event_count < 5:
            is_known = any(known in event_str for known in KNOWN_EVENTS)
            if not is_known:
                try:
                    event_data = event_obj.as_dict if hasattr(event_obj, "as_dict") else vars(event_obj)
                    with open(f"unknown_events_{username}.txt", "a", encoding="utf-8") as f:
                        f.write(f"=== イベント名: {event_str} ===\n時間: {datetime.now().strftime('%H:%M:%S')}\nデータ内容: {str(event_data)}\n\n")
                    unknown_event_count += 1
                    clean_name = event_str.split('.')[-1].replace("'>", "")
                    print(f"👀 未知のイベント【{clean_name}】を保存しました！ ({unknown_event_count}/5)")
                except Exception: pass

        return original_emit(event_name, *args, **kwargs)

    client.emit = custom_emit

    # ===============================
    # 💡 [新規追加] 同接人数の監視
    # ===============================
    # 💡【修正】同接人数の取得（m_total から取得するように修正）
    @client.on(RoomUserSeqEvent)
    async def on_room_user_seq(event: RoomUserSeqEvent):
        global max_viewers, viewer_counts
        try:
            # ログ解析から判明した `m_total` を優先的に取得
            current_viewers = getattr(event, 'm_total', None)
            
            # 念のため、他のバージョン用にフォールバックも残す
            if current_viewers is None:
                current_viewers = getattr(event, 'total', getattr(event, 'viewer_count', 0))
                
            if isinstance(current_viewers, int) and current_viewers > 0:
                max_viewers = max(max_viewers, current_viewers)
                viewer_counts.append(current_viewers)
        except Exception: 
            pass

    @client.on(ConnectEvent)
    async def on_connect(event: ConnectEvent):
        global start_time, session_id, total_likes, offline_time
        if offline_time and (datetime.now() - offline_time).total_seconds() < GRACE_PERIOD_SECONDS:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔄 再接続しました。集計を継続します。")
        else:
            start_time = datetime.now()
            total_likes = 0
            session_id = start_time.strftime("%Y%m%d_%H%M%S")
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 🚀 ライブ配信開始を検知！ (Session: {session_id})")
        offline_time = None

    @client.on(DisconnectEvent)
    async def on_disconnect(event: DisconnectEvent):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 🛑 配信終了、またはTikTok側から切断されました (DisconnectEvent)")
        try:
            client.stop()
        except: pass

    @client.on(JoinEvent)
    async def on_join(event: JoinEvent):
        global last_join_flush_time
        try:
            g_level = get_safe_g_level(event.user)
            if g_level >= 0: 
                current_time = datetime.now().strftime("%H:%M:%S")
                name = event.user.nickname
                user_id = getattr(event.user, 'user_id', getattr(event.user, 'id', getattr(event.user, 'unique_id', name)))
                
                user_join_counts[user_id] = user_join_counts.get(user_id, 0) + 1
                safe_session = session_id if session_id else "unknown_session"
                
                join_batch.append([safe_session, current_time, name, g_level, user_join_counts[user_id]])
                # 画面がうるさくなるのを防ぐため、Lv0の入室は画面に出さない（スプレッドシートには保存される）
                if g_level > 0:
                    print(f"[{current_time}] 🚪 {name}(Lv.{g_level}) が入室しました。")
                
                if time.time() - last_join_flush_time >= JOIN_FLUSH_INTERVAL:
                    flush_joins()
        except Exception: pass

    @client.on(BarrageEvent)
    async def on_barrage(event: BarrageEvent):
        global last_join_flush_time
        try:
            if not hasattr(event, 'user') or event.user is None: return
            g_level = get_safe_g_level(event.user)
            if g_level >= 25:
                current_time = datetime.now().strftime("%H:%M:%S")
                name = event.user.nickname
                user_id = getattr(event.user, 'user_id', getattr(event.user, 'id', getattr(event.user, 'unique_id', name)))
                
                user_join_counts[user_id] = user_join_counts.get(user_id, 0) + 1
                safe_session = session_id if session_id else "unknown_session"
                
                join_batch.append([safe_session, current_time, name, g_level, f"VIP登場({user_join_counts[user_id]})"])
                print(f"[{current_time}] 🏎️ VIPエフェクト入室: {name}(Lv.{g_level})")
                
                if time.time() - last_join_flush_time >= JOIN_FLUSH_INTERVAL:
                    flush_joins()
        except Exception: pass

    @client.on(GiftEvent)
    async def on_gift(event: GiftEvent):
        try:
            current_time = datetime.now().strftime("%H:%M:%S")
            name = event.user.nickname
            gift_name = event.gift.name
            g_level = get_safe_g_level(event.user)
            
            badge_lvl = getattr(event.user, 'badge_level', None)
            member_lvl = getattr(event.user, 'member_level', None)
            fan_level = badge_lvl if badge_lvl is not None else (member_lvl if member_lvl is not None else 0)
            
            if hasattr(event.gift, 'info') and event.gift.info is not None:
                coin_count = getattr(event.gift.info, 'coin_count', 0) or 0
                diamond_count = getattr(event.gift.info, 'diamond_count', getattr(event.gift.info, 'diamond', 0)) or 0
            else:
                coin_count = getattr(event.gift, 'coin_count', 0) or 0
                diamond_count = getattr(event.gift, 'diamond_count', getattr(event.gift, 'diamond', 0)) or 0
            
            safe_session = session_id if session_id else "unknown_session"
            write_to_sheet(ws_gifts, [safe_session, current_time, name, g_level, fan_level, gift_name, coin_count, diamond_count])
            print(f"[{current_time}] 🎁 {name}(Lv.{g_level}/Fan.{fan_level}) が {gift_name} を送信！")
        except Exception: pass

    @client.on(CommentEvent)
    async def on_comment(event: CommentEvent):
        global last_comment_flush_time
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
        except Exception: pass

    @client.on(FollowEvent)
    async def on_follow(event: FollowEvent):
        try:
            current_time = datetime.now().strftime("%H:%M:%S")
            name = event.user.nickname
            g_level = get_safe_g_level(event.user)
            safe_session = session_id if session_id else "unknown_session"
            write_to_sheet(ws_follows, [safe_session, current_time, name, g_level])
            print(f"[{current_time}] 👤 {name}(Lv.{g_level}) がフォローしました！")
        except Exception: pass

    @client.on(LikeEvent)
    async def on_like(event: LikeEvent):
        global total_likes
        try:
            if hasattr(event, 'total'): total_likes = event.total
            else: total_likes += getattr(event, 'count', 1)
        except Exception: pass

    # 実行
    try:
        client.run() 
        return True
    except UserOfflineError:
        return False
    except Exception as e:
        log_error(f"ライバー実行中 ({username})", e)
        return False
    finally:
        if session_id:
            finish_live_session()

# ==========================================
# 🔄 メイン巡回ループ
# ==========================================
if __name__ == "__main__":
    print("🔄 複数ライバー自動巡回システムを起動しました...")
    print(f"監視対象: {list(TARGET_USERS.keys())}")
    print("(Ctrl+C で安全に停止できます)\n")
    
    threading.Thread(target=watchdog, daemon=True).start()
    
    while True:
        live_detected = False
        
        for username, spreadsheet_id in TARGET_USERS.items():
            is_live = run_monitor(username, spreadsheet_id)
            if is_live:
                live_detected = True
                break 
        
        if not live_detected:
            current_time = datetime.now().strftime("%H:%M:%S")
            print(f"[{current_time}] 💤 全員オフラインです。60秒後に再チェックします...")
            time.sleep(60)
        else:
            print("\n💤 サイレント切断対策のため、15秒待機してから巡回を再開します...")
            time.sleep(15)