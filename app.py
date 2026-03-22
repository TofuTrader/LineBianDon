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
    """回傳 [{user_id, name, line_id}] 的清單"""
    sh = get_spreadsheet()
    try:
        ws = sh.worksheet("使用者清單")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet("使用者清單", rows=200, cols=3)
        ws.append_row(["user_id", "姓名", "加入日期"])
        return []
    records = ws.get_all_records()
    return records

def get_user_by_id(user_id: str):
    users = get_all_users()
    for u in users:
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
def get_balance(name: str):
    sh = get_spreadsheet()
    try:
        ws = sh.worksheet("儲值總表")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet("儲值總表", rows=200, cols=2)
        ws.append_row(["姓名", "餘額"])
        return 0
    records = ws.get_all_records()
    for r in records:
        if str(r.get("姓名")) == str(name):
            return int(r.get("餘額", 0))
    return 0

def deduct_balance(name: str, amount: int):
    sh = get_spreadsheet()
    ws = sh.worksheet("儲值總表")
    records = ws.get_all_records()
    for i, r in enumerate(records):
        if str(r.get("姓名")) == str(name):
            row_num = i + 2  # header is row 1
            current = int(r.get("餘額", 0))
            ws.update_cell(row_num, 2, current - amount)
            return current - amount
    return 0

# ── 今日菜單（記憶體暫存）────────────────────────────
today_menu = {}  # {"sender_name": str, "items": [{"name": str, "price": int}], "raw": str}

def parse_menu(text: str):
    """
    解析菜單格式：
    【菜單】雞腿飯 80 / 排骨飯 75 / 素食便當 70
    回傳 [{"name": "雞腿飯", "price": 80}, ...]
    """
    # 去掉【菜單】前綴
    text = re.sub(r"[【\[]\s*菜單\s*[】\]]", "", text).strip()
    items = []
    # 以 / 或換行分割
    parts = re.split(r"[/／\n]", text)
    for part in parts:
        part = part.strip()
        if not part:
            continue
        # 取最後一個數字作為價格
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
    lines.append("不訂請回覆：不訂")
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

def add_order(name: str, item_name: str, price: int):
    ws = ensure_order_sheet()
    today = get_today_str()
    now_str = get_taiwan_now().strftime("%Y-%m-%d %H:%M:%S")

    # 找今天同一個人的訂單，超過 1 筆就把前面的標為銷單
    records = ws.get_all_records()
    today_orders_rows = []
    for i, r in enumerate(records):
        if str(r.get("日期")) == today and str(r.get("姓名")) == name and str(r.get("狀態")) == "正常":
            today_orders_rows.append(i + 2)  # row number (1-indexed, header=1)

    # 把已存在的正常訂單標為銷單
    for row_num in today_orders_rows:
        ws.update_cell(row_num, 5, "銷單")

    # 新增新訂單
    ws.append_row([today, name, item_name, price, "正常", now_str])

def get_today_valid_orders():
    """取得今天所有正常訂單"""
    ws = ensure_order_sheet()
    today = get_today_str()
    records = ws.get_all_records()
    return [r for r in records if str(r.get("日期")) == today and str(r.get("狀態")) == "正常"]

# ── 等待狀態（記憶體）────────────────────────────────
# 格式：{user_id: "register" | "ordered"}
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
    """使用者加好友時"""
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
        # 確認沒有重複
        existing = get_user_by_id(user_id)
        if existing:
            user_state.pop(user_id, None)
            reply(f"你已登記為「{existing['姓名']}」，無需重複登記。")
            return
        register_user(user_id, text)
        user_state.pop(user_id, None)
        reply(f"登記完成！歡迎 {text}！\n之後每天菜單發出時，你會收到通知。")
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

        today_menu["sender_name"] = name
        today_menu["items"] = items
        today_menu["raw"] = text

        # 廣播給所有使用者
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

    # ── 訂便當 ────────────────────────────────────────
    if text in ["不訂", "不用", "不要", "no", "No"]:
        reply("好的，今天不訂便當 👌")
        return

    if not today_menu.get("items"):
        reply("今天還沒有人發菜單喔，等菜單發出後再訂購。")
        return

    items = today_menu["items"]
    chosen = None

    # 判斷是數字編號還是名稱
    if re.match(r"^\d+$", text):
        idx = int(text) - 1
        if 0 <= idx < len(items):
            chosen = items[idx]
        else:
            reply(f"請輸入 1 到 {len(items)} 的編號")
            return
    else:
        # 模糊比對名稱
        for item in items:
            if text in item["name"] or item["name"] in text:
                chosen = item
                break
        if not chosen:
            item_list = "\n".join([f"{i+1}. {it['name']}" for i, it in enumerate(items)])
            reply(f"找不到「{text}」，請用編號或正確名稱訂購：\n{item_list}")
            return

    # 記錄訂單
    add_order(name, chosen["name"], chosen["price"])
    balance = get_balance(name)
    price_str = f"${chosen['price']}" if chosen["price"] > 0 else "（待確認）"
    reply(
        f"✅ 訂購成功！\n"
        f"姓名：{name}\n"
        f"便當：{chosen['name']} {price_str}\n"
        f"目前儲值餘額：${balance}\n\n"
        f"（若要換訂，直接重新回覆便當名稱即可，舊的會自動銷單）"
    )

# ── 10 點自動結單 ─────────────────────────────────────
def close_orders():
    """每天 10 點自動結單、統計、扣款、發通知"""
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

    # 結單通知（廣播）
    summary_lines = [f"🔔 今日結單通知（{get_today_str()}）\n"]
    for item_name, stats in item_summary.items():
        summary_lines.append(f"▪ {item_name}：{stats['count']} 個，小計 ${stats['total']}")
    summary_lines.append(f"\n📊 合計：{len(orders)} 個便當，總金額 ${total_amount}")
    summary_text = "\n".join(summary_lines)

    # 對每個有訂便當的人發個人通知，並扣款
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
        remaining = deduct_balance(name, total_person)

        order_lines = [f"🍱 {name} 今日訂單：\n"]
        for o in p_orders:
            order_lines.append(f"▪ {o['便當']}：${o['金額']}")
        order_lines.append(f"\n本次扣款：${total_person}")
        order_lines.append(f"儲值餘額剩餘：${remaining}")
        personal_text = "\n".join(order_lines)

        uid = uid_map.get(name)
        if uid:
            try:
                send_message(uid, personal_text)
            except Exception:
                pass

    # 廣播整體結單統計（給所有人）
    all_users = get_all_users()
    for u in all_users:
        uid = str(u.get("user_id", ""))
        if uid:
            try:
                send_message(uid, summary_text)
            except Exception:
                pass

    # 清空今日菜單
    today_menu.clear()

# ── 排程設定 ──────────────────────────────────────────
tz = pytz.timezone(TIMEZONE)
scheduler = BackgroundScheduler(timezone=tz)
scheduler.add_job(close_orders, "cron", hour=CLOSE_HOUR, minute=0)
scheduler.start()

# ── 啟動 ──────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
