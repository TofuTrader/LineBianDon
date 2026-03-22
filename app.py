import os
import json
import re
from datetime import datetime, date
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, PushMessageRequest,
    TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, FollowEvent
from apscheduler.schedulers.background import BackgroundScheduler
import gspread
from google.oauth2.service_account import Credentials
import pytz

app = Flask(__name__)

# ── 環境變數 ──────────────────────────────────────────
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "")
CLOSE_HOUR = int(os.environ.get("CLOSE_HOUR", "10"))  # 結單時間（預設 10 點）
TIMEZONE = os.environ.get("TIMEZONE", "Asia/Taipei")

# ── Line SDK 設定 ──────────────────────────────────────
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ── Google Sheets 設定 ────────────────────────────────
def get_sheets_client():
    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(creds)

def get_spreadsheet():
    gc = get_sheets_client()
    return gc.open_by_key(SPREADSHEET_ID)

# ── 工具函數 ──────────────────────────────────────────
def get_taiwan_now():
    tz = pytz.timezone(TIMEZONE)
    return datetime.now(tz)

def get_today_str():
    return get_taiwan_now().strftime("%Y-%m-%d")

def get_line_api():
    return MessagingApi(ApiClient(configuration))

def send_message(user_id: str, text: str):
    api = get_line_api()
    api.push_message(PushMessageRequest(
        to=user_id,
        messages=[TextMessage(text=text)]
    ))

# ── 使用者資料（Sheet: 使用者清單）────────────────────
def get_all_users():
    sh = get_spreadsheet()
    try:
        ws = sh.worksheet("使用者清單")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet("使用者清單", rows=200, cols=3)
        ws.append_row(["user_id", "姓名", "加入日期"])
        return []
    return ws.get_all_records()

def get_user_by_id(user_id: str):
    for u in get_all_users():
        if str(u.get("user_id")) == str(user_id):
            return u
    return None

def register_user(user_id: str, name: str):
    sh = get_spreadsheet()
    try:
        ws = sh.worksheet("使用者清單")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet("使用者清單", rows=200, cols=3)
        ws.append_row(["user_id", "姓名", "加入日期"])
    ws.append_row([user_id, name, get_today_str()])

# ── 儲值總表（Sheet: 儲值總表）───────────────────────
# 欄位：姓名｜前日餘額｜今日餘額
# 邏輯：
#   - 扣款只動「今日餘額」
#   - 每天午夜 00:00 把「今日餘額」複製到「前日餘額」（隔日更新）
#   - 手動儲值時直接更新「今日餘額」即可（前日餘額次日自動跟上）

BALANCE_SHEET_HEADER = ["姓名", "前日餘額", "今日餘額"]
COL_NAME     = 1
COL_PREV_BAL = 2
COL_TODAY_BAL = 3

def ensure_balance_sheet():
    sh = get_spreadsheet()
    try:
        ws = sh.worksheet("儲值總表")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet("儲值總表", rows=200, cols=3)
        ws.append_row(BALANCE_SHEET_HEADER)
    return ws

def _migrate_balance_sheet_if_needed(ws):
    """
    舊版只有兩欄（姓名、餘額），自動補上前日餘額欄位。
    """
    headers = ws.row_values(1)
    if len(headers) < 3 or headers[1] != "前日餘額":
        # 插入新 header
        ws.update("A1:C1", [BALANCE_SHEET_HEADER])
        # 把原本第二欄（餘額）的值複製到第三欄（今日餘額），第二欄改為前日餘額同值
        records = ws.get_all_values()[1:]  # skip header
        for i, row in enumerate(records):
            row_num = i + 2
            old_bal = row[1] if len(row) > 1 else "0"
            ws.update_cell(row_num, COL_PREV_BAL, old_bal)
            ws.update_cell(row_num, COL_TODAY_BAL, old_bal)

def get_balance(name: str) -> int:
    """讀取今日餘額"""
    ws = ensure_balance_sheet()
    _migrate_balance_sheet_if_needed(ws)
    for r in ws.get_all_records():
        if str(r.get("姓名")) == str(name):
            return int(r.get("今日餘額", 0))
    return 0

def deduct_balance(name: str, amount: int) -> int:
    """扣除今日餘額，回傳扣後今日餘額"""
    ws = ensure_balance_sheet()
    _migrate_balance_sheet_if_needed(ws)
    records = ws.get_all_records()
    for i, r in enumerate(records):
        if str(r.get("姓名")) == str(name):
            row_num = i + 2
            current = int(r.get("今日餘額", 0))
            new_bal = current - amount
            ws.update_cell(row_num, COL_TODAY_BAL, new_bal)
            return new_bal
    return 0

def reset_daily_balances():
    """
    每天午夜執行：把今日餘額複製到前日餘額，做為次日的參考基準。
    """
    ws = ensure_balance_sheet()
    _migrate_balance_sheet_if_needed(ws)
    records = ws.get_all_records()
    for i, r in enumerate(records):
        row_num = i + 2
        today_bal = r.get("今日餘額", 0)
        ws.update_cell(row_num, COL_PREV_BAL, today_bal)

# ── 今日菜單（記憶體暫存）────────────────────────────
today_menu = {}

def parse_menu(text: str):
    text = re.sub(r"[【\[]\s*菜單\s*[】\]]", "", text).strip()
    items = []
    parts = re.split(r"[/／\n]", text)
    for part in parts:
        part = part.strip()
        if not part:
            continue
        m = re.search(r"(.+?)\s+(\d+)\s*$", part)
        if m:
            items.append({"name": m.group(1).strip(), "price": int(m.group(2))})
        else:
            items.append({"name": part, "price": 0})
    return items

def format_menu_broadcast(sender_name: str, items: list) -> str:
    lines = [f"🍱 {sender_name} 今日菜單：\n"]
    for i, item in enumerate(items, 1):
        price_str = f"${item['price']}" if item['price'] > 0 else "（價格未標示）"
        lines.append(f"{i}. {item['name']} {price_str}")
    lines.append("\n請回覆便當名稱或編號訂購，例如：雞腿飯 或 1")
    lines.append("不訂或取消請回覆：不訂")
    return "\n".join(lines)

# ── 訂單（Sheet: 訂單紀錄）───────────────────────────
def ensure_order_sheet():
    sh = get_spreadsheet()
    try:
        ws = sh.worksheet("訂單紀錄")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet("訂單紀錄", rows=1000, cols=6)
        ws.append_row(["日期", "姓名", "便當", "金額", "狀態", "時間戳記"])
    return ws

def cancel_today_orders(name: str, reason: str = "銷單"):
    """
    把今天此人所有正常訂單標為指定狀態，回傳被取消的訂單列表。
    reason:
      "銷單"    — 被新訂單取代（系統自動）
      "主動取消" — 使用者明確輸入取消指令
    """
    ws = ensure_order_sheet()
    today = get_today_str()
    now_str = get_taiwan_now().strftime("%Y-%m-%d %H:%M:%S")
    records = ws.get_all_records()
    cancelled_orders = []
    for i, r in enumerate(records):
        if str(r.get("日期")) == today and str(r.get("姓名")) == name and str(r.get("狀態")) == "正常":
            ws.update_cell(i + 2, 5, reason)
            cancelled_orders.append(r)
    return cancelled_orders

def add_order(name: str, item_name: str, price: int):
    ws = ensure_order_sheet()
    today = get_today_str()
    now_str = get_taiwan_now().strftime("%Y-%m-%d %H:%M:%S")
    # 先把今天舊訂單全部標為「銷單」（被新訂單取代）
    cancel_today_orders(name, reason="銷單")
    # 新增新訂單
    ws.append_row([today, name, item_name, price, "正常", now_str])

def has_today_order(name: str) -> bool:
    """判斷此人今天是否有正常訂單"""
    ws = ensure_order_sheet()
    today = get_today_str()
    records = ws.get_all_records()
    return any(
        str(r.get("日期")) == today and str(r.get("姓名")) == name and str(r.get("狀態")) == "正常"
        for r in records
    )

def get_today_valid_orders():
    ws = ensure_order_sheet()
    today = get_today_str()
    records = ws.get_all_records()
    return [r for r in records if str(r.get("日期")) == today and str(r.get("狀態")) == "正常"]

def mark_orders_closed():
    """結單後把今天所有「正常」訂單標為「結單」，避免下一輪統計抓到"""
    ws = ensure_order_sheet()
    today = get_today_str()
    records = ws.get_all_records()
    for i, r in enumerate(records):
        if str(r.get("日期")) == today and str(r.get("狀態")) == "正常":
            ws.update_cell(i + 2, 5, "結單")

# ── 等待狀態（記憶體）────────────────────────────────
user_state = {}

# ── Webhook ───────────────────────────────────────────
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

@handler.add(FollowEvent)
def on_follow(event):
    user_id = event.source.user_id
    api = get_line_api()
    api.reply_message(ReplyMessageRequest(
        reply_token=event.reply_token,
        messages=[TextMessage(text="歡迎加入便當訂購機器人！\n\n請先告訴我你的姓名（直接輸入即可），完成登記後就可以收到每日菜單通知。")]
    ))
    user_state[user_id] = "register"

@handler.add(MessageEvent, message=TextMessageContent)
def on_message(event):
    user_id = event.source.user_id
    text = event.message.text.strip()
    api = get_line_api()

    def reply(msg: str):
        api.reply_message(ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[TextMessage(text=msg)]
        ))

    # ── 等待登記姓名 ──────────────────────────────────
    if user_state.get(user_id) == "register":
        if len(text) < 1 or len(text) > 10:
            reply("姓名請輸入 1-10 個字")
            return
        existing = get_user_by_id(user_id)
        if existing:
            user_state.pop(user_id, None)
            reply(f"你已登記為「{existing['姓名']}」，無需重複登記。")
            return
        register_user(user_id, text)
        user_state.pop(user_id, None)
        reply(f"登記完成！歡迎 {text}！\n之後每天菜單發出時，你會收到通知。\n\n輸入「說明」可查看完整操作指南。")
        return

    # ── 未登記使用者 ──────────────────────────────────
    user_info = get_user_by_id(user_id)
    if not user_info:
        reply("你還沒登記姓名喔！請輸入你的姓名完成登記。")
        user_state[user_id] = "register"
        return

    name = str(user_info["姓名"])

    # ── 發布菜單 ──────────────────────────────────────
    menu_keywords = ["【菜單】", "[菜單]", "菜單：", "今日菜單"]
    is_menu = any(kw in text for kw in menu_keywords)

    if is_menu:
        items = parse_menu(text)
        if not items:
            reply("菜單格式不對，請用這個格式：\n【菜單】雞腿飯 80 / 排骨飯 75")
            return

        # ── 已有進行中菜單時，擋下並提示 ─────────────────
        if today_menu.get("items"):
            existing_sender = today_menu.get("sender_name", "不明")
            existing_orders = get_today_valid_orders()
            order_count = len(existing_orders)
            reply(
                f"⚠️ 目前已有 {existing_sender} 發布的菜單，共 {order_count} 筆有效訂單尚未結單。\n\n"
                f"請先輸入「結單」完成本輪訂購，結單後即可發布新菜單。"
            )
            return

        today_menu["sender_name"] = name
        today_menu["items"] = items
        today_menu["raw"] = text

        broadcast_text = format_menu_broadcast(name, items)
        all_users = get_all_users()
        sent_count = 0
        for u in all_users:
            uid = str(u.get("user_id", ""))
            if uid and uid != user_id:
                try:
                    send_message(uid, broadcast_text)
                    sent_count += 1
                except Exception:
                    pass

        reply(f"✅ 菜單已廣播給 {sent_count} 位成員！\n\n{broadcast_text}")
        return

    # ── 提前結單 ──────────────────────────────────────
    if text in ["結單", "提前結單", "現在結單", "結束訂購", "close"]:
        orders = get_today_valid_orders()
        if not orders:
            reply("目前沒有有效訂單，無法結單。")
            return
        # 廣播結單預告
        all_users = get_all_users()
        for u in all_users:
            uid = str(u.get("user_id", ""))
            if uid and uid != user_id:
                try:
                    send_message(uid, f"🔔 {name} 發起提前結單，正在處理訂單...")
                except Exception:
                    pass
        reply(f"🔔 正在結單，共 {len(orders)} 筆訂單，稍後發送個人通知...")
        close_orders()
        return

    # ── 操作說明 ──────────────────────────────────────
    if text in ["說明", "指令", "help", "Help", "HELP", "?", "？", "操作說明"]:
        close_h = CLOSE_HOUR
        help_text = (
            "📖 便當訂購機器人 — 操作說明\n"
            "─────────────────────\n\n"
            "🍱 【訂便當】\n"
            "收到菜單廣播後，直接回覆便當名稱或編號：\n"
            "  例：雞腿飯  或  1\n"
            "同一天重複回覆，以最後一筆為準（前筆自動銷單）\n\n"
            "❌ 【取消訂單】\n"
            "回覆以下任一字即可取消今日訂單：\n"
            "  不訂 ／ 取消 ／ 不要\n"
            "取消後仍可重新訂購\n\n"
            "💰 【查詢餘額】\n"
            "回覆：餘額  或  查餘額\n"
            "可查看前日餘額與今日餘額\n\n"
            "📋 【發布菜單（任何人皆可）】\n"
            "格式：【菜單】便當名稱 價錢 / 便當名稱 價錢\n"
            "  例：【菜單】雞腿飯 80 / 排骨飯 75 / 素食 70\n"
            "機器人會自動廣播給所有成員\n\n"
            "📊 【查詢目前統計】\n"
            "回覆：查統計  或  統計\n"
            "顯示每個人點了什麼、各便當數量與總金額\n\n"
            f"🔔 【結單時間】每天 {close_h}:00 自動結單\n"
            "結單後發送個人通知（訂了什麼、扣多少、剩多少）\n\n"
            "⏱ 【提前結單】\n"
            "回覆：結單  或  提前結單\n"
            "立即結單並扣款，結單後可再發新菜單開啟下一輪\n\n"
            "─────────────────────\n"
            "指令速查：說明 ／ 統計 ／ 餘額 ／ 不訂 ／ 取消 ／ 結單"
        )
        reply(help_text)
        return

    # ── 查詢目前訂單統計 ──────────────────────────────
    if text in ["查統計", "統計", "現在訂單", "目前訂單", "查訂單"]:
        orders = get_today_valid_orders()
        if not orders:
            reply("今天還沒有人訂便當喔 🍱")
            return

        # 各人訂單
        person_orders = {}
        for o in orders:
            n = o["姓名"]
            if n not in person_orders:
                person_orders[n] = []
            person_orders[n].append(o)

        # 各便當統計
        item_summary = {}
        for o in orders:
            key = o["便當"]
            if key not in item_summary:
                item_summary[key] = {"count": 0, "total": 0}
            item_summary[key]["count"] += 1
            item_summary[key]["total"] += int(o.get("金額", 0))

        total_amount = sum(int(o.get("金額", 0)) for o in orders)

        lines = [f"📋 目前訂單統計（結單前可更改）\n"]

        # 每個人點了什麼
        lines.append("👥 個人訂購明細：")
        for n, p_orders in person_orders.items():
            items_str = "、".join(
                [f"{o['便當']}（${o['金額']}）" for o in p_orders]
            )
            person_total = sum(int(o.get("金額", 0)) for o in p_orders)
            lines.append(f"  {n}：{items_str}　共 ${person_total}")

        lines.append("")

        # 各便當數量
        lines.append("🍱 各便當統計：")
        for item_name, stats in item_summary.items():
            lines.append(f"  {item_name}：{stats['count']} 個，小計 ${stats['total']}")

        lines.append(f"\n💰 總計：{len(orders)} 個便當，總金額 ${total_amount}")
        reply("\n".join(lines))
        return

    # ── 查詢餘額 ──────────────────────────────────────
    if text in ["餘額", "查餘額", "查詢餘額", "balance"]:
        prev_bal = get_balance_prev(name)
        today_bal = get_balance(name)
        reply(
            f"💰 {name} 的儲值餘額\n"
            f"前日餘額：${prev_bal}\n"
            f"今日餘額：${today_bal}\n"
            f"（今日餘額於每天 {CLOSE_HOUR}:00 結單時扣款）"
        )
        return

    # ── 取消訂購 ──────────────────────────────────────
    if text in ["不訂", "不用", "不要", "取消", "cancel", "no", "No"]:
        cancelled_orders = cancel_today_orders(name, reason="主動取消")
        if cancelled_orders:
            items_str = "、".join([o["便當"] for o in cancelled_orders])
            balance = get_balance(name)
            reply(
                f"✅ 已取消今日訂單：{items_str}\n"
                f"（已記錄為主動取消）\n"
                f"今日餘額：${balance}（結單前不扣款）\n\n"
                f"若要重新訂購，直接回覆便當名稱即可。"
            )
        else:
            reply("你今天還沒有訂便當喔 👌")
        return

    # ── 訂便當 ────────────────────────────────────────
    if not today_menu.get("items"):
        reply("今天還沒有人發菜單喔，等菜單發出後再訂購。")
        return

    items = today_menu["items"]
    chosen = None

    if re.match(r"^\d+$", text):
        idx = int(text) - 1
        if 0 <= idx < len(items):
            chosen = items[idx]
        else:
            reply(f"請輸入 1 到 {len(items)} 的編號")
            return
    else:
        for item in items:
            if text in item["name"] or item["name"] in text:
                chosen = item
                break
        if not chosen:
            item_list = "\n".join([f"{i+1}. {it['name']}" for i, it in enumerate(items)])
            reply(f"找不到「{text}」，請用編號或正確名稱訂購：\n{item_list}")
            return

    had_previous = has_today_order(name)
    add_order(name, chosen["name"], chosen["price"])
    balance = get_balance(name)
    price_str = f"${chosen['price']}" if chosen["price"] > 0 else "（待確認）"

    change_note = "（已自動取消舊訂單）\n" if had_previous else ""
    reply(
        f"✅ 訂購成功！{change_note}\n"
        f"姓名：{name}\n"
        f"便當：{chosen['name']} {price_str}\n"
        f"前日餘額：${get_balance_prev(name)}\n"
        f"今日餘額：${balance}（結單前不扣款）"
    )

# ── 讀取前日餘額 ──────────────────────────────────────
def get_balance_prev(name: str) -> int:
    ws = ensure_balance_sheet()
    for r in ws.get_all_records():
        if str(r.get("姓名")) == str(name):
            return int(r.get("前日餘額", 0))
    return 0

# ── 10 點自動結單 ─────────────────────────────────────
def close_orders():
    """每天 CLOSE_HOUR 點自動結單、統計、扣今日餘額、發通知"""
    orders = get_today_valid_orders()
    if not orders:
        return

    # 統計各便當數量
    item_summary = {}
    for o in orders:
        key = o["便當"]
        if key not in item_summary:
            item_summary[key] = {"count": 0, "total": 0}
        item_summary[key]["count"] += 1
        item_summary[key]["total"] += int(o.get("金額", 0))

    total_amount = sum(int(o.get("金額", 0)) for o in orders)

    summary_lines = [f"🔔 今日結單通知（{get_today_str()}）\n"]

    # 各人訂購明細
    summary_lines.append("👥 個人訂購明細：")
    for n, p_orders in {o["姓名"]: [] for o in orders}.items():
        pass  # 先建好順序，下面重新處理
    person_orders_tmp = {}
    for o in orders:
        person_orders_tmp.setdefault(o["姓名"], []).append(o)
    for n, p_orders in person_orders_tmp.items():
        items_str = "、".join([f"{o['便當']}(${o['金額']})" for o in p_orders])
        person_total = sum(int(o.get("金額", 0)) for o in p_orders)
        summary_lines.append(f"  {n}：{items_str}　共 ${person_total}")

    # 各便當統計
    summary_lines.append("")
    summary_lines.append("🍱 便當統計：")
    for item_name, stats in item_summary.items():
        summary_lines.append(f"  {item_name}：{stats['count']} 個，小計 ${stats['total']}")

    summary_lines.append(f"\n💰 合計：{len(orders)} 個便當，總金額 ${total_amount}")
    summary_text = "\n".join(summary_lines)

    # 對每個有訂便當的人扣款並發通知
    person_orders = {}
    for o in orders:
        n = o["姓名"]
        if n not in person_orders:
            person_orders[n] = []
        person_orders[n].append(o)

    all_users = get_all_users()
    uid_map = {str(u["姓名"]): str(u["user_id"]) for u in all_users}

    for name, p_orders in person_orders.items():
        total_person = sum(int(o.get("金額", 0)) for o in p_orders)
        prev_bal = get_balance_prev(name)   # 前日餘額（今日結單前不變）
        remaining = deduct_balance(name, total_person)  # 扣今日餘額

        order_lines = [f"🍱 {name} 今日訂單：\n"]
        for o in p_orders:
            order_lines.append(f"▪ {o['便當']}：${o['金額']}")
        order_lines.append(f"\n本次扣款：${total_person}")
        order_lines.append(f"前日餘額：${prev_bal}")
        order_lines.append(f"今日餘額（扣後）：${remaining}")
        personal_text = "\n".join(order_lines)

        uid = uid_map.get(name)
        if uid:
            try:
                send_message(uid, personal_text)
            except Exception:
                pass

    # 廣播整體結單統計
    for u in all_users:
        uid = str(u.get("user_id", ""))
        if uid:
            try:
                send_message(uid, summary_text)
            except Exception:
                pass

    # 把今天所有正常訂單標為「結單」
    mark_orders_closed()
    today_menu.clear()

# ── 排程設定 ──────────────────────────────────────────
tz = pytz.timezone(TIMEZONE)
scheduler = BackgroundScheduler(timezone=tz)
scheduler.add_job(close_orders, "cron", hour=CLOSE_HOUR, minute=0)
# 每天午夜把今日餘額更新為前日餘額
scheduler.add_job(reset_daily_balances, "cron", hour=0, minute=0)
scheduler.start()

# ── 啟動 ──────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
