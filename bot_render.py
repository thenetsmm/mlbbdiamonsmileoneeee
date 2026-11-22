import os
import re
import json
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
import smile_client as sc

TOKEN = os.environ.get("BOT_TOKEN")
SECRET = os.environ.get("WEBHOOK_SECRET")
BASE_URL = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("PUBLIC_URL")
PORT = int(os.environ.get("PORT", "10000"))
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0") or 0)
MONGODB_URI = os.environ.get("MONGODB_URI")
# BUY_ENABLED=1 -> real purchase. Otherwise DRY-RUN (player check + price + balance only).
BUY_ENABLED = os.environ.get("BUY_ENABLED", "0") == "1"
MAX_ORDER_COINS = float(os.environ.get("MAX_ORDER_COINS", "100"))
MAX_PER_DAY = int(os.environ.get("MAX_PER_DAY", "3"))
CONFIRM_SECONDS = 120
BUY_LOCK = threading.Lock()
# Must match the Chrome that created the session (cookies can be tied to the UA).
SESSION_UA = os.environ.get(
    "SESSION_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36",
)
WATCH_MINUTES = int(os.environ.get("SESSION_WATCH_MINUTES", "30"))

if not TOKEN or not SECRET:
    raise SystemExit("BOT_TOKEN and WEBHOOK_SECRET environment variables are required")

API = f"https://api.telegram.org/bot{TOKEN}"


def send(chat_id, text, keyboard=None):
    payload = {"chat_id": chat_id, "text": text}
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    requests.post(f"{API}/sendMessage", json=payload, timeout=20)


def tg(method, **payload):
    try:
        return requests.post(f"{API}/{method}", json=payload, timeout=20)
    except requests.RequestException:
        traceback.print_exc()


def is_admin(from_id):
    return bool(ADMIN_ID) and from_id == ADMIN_ID


def mask(x):
    return x[:3] + "***" + x[-2:] if len(x) > 5 else "***"


# ---------- MongoDB ----------
_mongo = {"db": None}


def db():
    if _mongo["db"] is None:
        if not MONGODB_URI:
            raise RuntimeError("MONGODB_URI is not set")
        from pymongo import MongoClient
        _mongo["db"] = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=8000)["smileone"]
    return _mongo["db"]


def state_col():
    return db()["bot_state"]


def orders_col():
    return db()["buy_orders"]


def save_session(cookies):
    state_col().update_one(
        {"_id": "session"},
        {"$set": {"cookies": cookies, "updated": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())}},
        upsert=True)


def load_session():
    return state_col().find_one({"_id": "session"})


STATUS_TEXT = {
    "ok": "Logged in, session works ✅",
    "expired": "Not logged in (session expired, or the site rejects Render's IP/browser)",
    "challenge": "Cloudflare challenge shown (Render was rejected)",
    "unknown": "Unclear - page did not look like a logged-in account page",
}


def live_session():
    """Returns (requests session, balance, None) if OK, else (None, None, message)."""
    doc = load_session()
    if not doc:
        return None, None, "No session yet. Send smileone_session.json to this chat as a file."
    s = sc.make_session(doc["cookies"], SESSION_UA)
    code, bal = sc.inspect(s)
    if code != "ok":
        return None, None, "Session not working: " + STATUS_TEXT.get(code, code)
    return s, bal, None


def check_session_text():
    doc = load_session()
    if not doc:
        return "No session yet. Send smileone_session.json to this chat as a file."
    s = sc.make_session(doc["cookies"], SESSION_UA)
    code, bal = sc.inspect(s)
    return (f"Session saved: {doc.get('updated')}\n"
            f"Result: {STATUS_TEXT.get(code, code)}\n"
            + (f"Smile Coins balance: {bal}" if bal is not None else ""))


# ---------- session watcher ----------
def watch_decision(last, code):
    if code == last:
        return None
    if code in ("expired", "challenge"):
        return ("⚠️ Smile One session stopped working.\n"
                "Log in again in the debug Chrome on your laptop, run save_session_smile.py "
                "and send the new smileone_session.json to this chat.")
    if code == "ok" and last in ("expired", "challenge", "unknown"):
        return "✅ Smile One session works again."
    return None


def session_watch_loop():
    time.sleep(60)
    while True:
        try:
            if ADMIN_ID and MONGODB_URI:
                doc = load_session()
                if doc:
                    code, _ = sc.inspect(sc.make_session(doc["cookies"], SESSION_UA))
                    meta = state_col().find_one({"_id": "watch"}) or {}
                    msg = watch_decision(meta.get("last"), code)
                    if msg:
                        send(ADMIN_ID, msg)
                    if code != meta.get("last"):
                        state_col().update_one({"_id": "watch"}, {"$set": {"last": code}}, upsert=True)
        except Exception:
            traceback.print_exc()
        time.sleep(WATCH_MINUTES * 60)


# ---------- commands ----------
def packages_text():
    s, _, msg = live_session()
    if not s:
        return msg
    _, prices = sc.get_page(s)
    rows = []
    for pid, name in sc.PACKAGES.items():
        star = " ⚠️" if pid in sc.LIMITED else ""
        rows.append(f"{pid}  {name}{star}\n     page {prices.get(pid, '?')} / up to {sc.MAX_COINS[pid]}")
    return ("Smile One MLBB (Smile Coins)\n'up to' = worst-case charge used for limits\n"
            "⚠️ = limited / level-restricted\n\n" + "\n".join(rows))


def paid_today():
    start = time.time() - 24 * 3600
    return orders_col().count_documents({"status": {"$in": ["paid", "unknown"]}, "created": {"$gt": start}})


def cmd_buy(chat_id, from_id, arg):
    if not is_admin(from_id):
        send(chat_id, "Admin only.")
        return
    parts = arg.split()
    if len(parts) != 3:
        send(chat_id, "Usage: /buy <package id> <User ID> <Zone ID>\nExample: /buy 13 824412642 12267\nSee /packages")
        return
    pid, uid, zone = parts
    if pid not in sc.PACKAGES:
        send(chat_id, "Package not allowed. See /packages")
        return
    if not re.fullmatch(r"\d{5,12}", uid) or not re.fullmatch(r"\d{1,6}", zone):
        send(chat_id, "Check User ID (5-12 digits) and Zone ID (1-6 digits).")
        return
    if BUY_ENABLED and paid_today() >= MAX_PER_DAY:
        send(chat_id, f"Daily limit reached ({MAX_PER_DAY} in 24h).")
        return
    s, bal, msg = live_session()
    if not s:
        send(chat_id, msg)
        return
    csrf, prices = sc.get_page(s)
    shown = prices.get(pid)
    if shown is None:
        send(chat_id, "Could not read the price. Not buying.")
        return
    price = max(shown, sc.MAX_COINS[pid])
    if price > MAX_ORDER_COINS:
        send(chat_id, f"Price {price} is above MAX_ORDER_COINS ({MAX_ORDER_COINS}). Change it on Render if intended.")
        return
    if bal < price:
        send(chat_id, f"Not enough Smile Coins: balance {bal}, price {price}.")
        return
    name = sc.check_role(s, csrf, uid, zone, pid)
    exp = sc.expected_price(pid, shown)
    token = uuid.uuid4().hex[:10]
    orders_col().insert_one({"_id": token, "status": "pending", "created": time.time(),
                             "pid": pid, "price": price, "user_id": uid, "zone_id": zone})
    mode = "🟥 REAL purchase" if BUY_ENABLED else "🧪 DRY-RUN (nothing will be bought)"
    send(chat_id,
         f"Confirm\n\nPlayer: {name}\nUser ID: {uid}  Zone: {zone}\nPackage: {sc.PACKAGES[pid]}\n"
         f"Price: page {shown} / expected {exp if exp is not None else '?'} / up to {price} Smile Coins\nBalance: {bal}\nMode: {mode}\n"
         + ("⚠️ Limited pack: once-per-week/month or level 5+ rules may apply.\n" if pid in sc.LIMITED else "")
         + "\n"
         f"Wrong ID cannot be refunded. Tap within {CONFIRM_SECONDS}s.",
         keyboard=[[{"text": "✅ Confirm", "callback_data": f"buy:ok:{token}"},
                    {"text": "❌ Cancel", "callback_data": f"buy:no:{token}"}]])


def handle_callback(cb):
    chat_id = None
    try:
        from_id = (cb.get("from") or {}).get("id")
        msg = cb.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        message_id = msg.get("message_id")
        tg("answerCallbackQuery", callback_query_id=cb["id"])
        if not chat_id or not is_admin(from_id):
            return
        try:
            _, action, token = (cb.get("data") or "").split(":")
        except ValueError:
            return
        col = orders_col()
        tg("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id, reply_markup={"inline_keyboard": []})
        if action == "no":
            col.find_one_and_update({"_id": token, "status": "pending"}, {"$set": {"status": "cancelled"}})
            send(chat_id, "Cancelled. Nothing bought.")
            return
        if action != "ok":
            return
        # single use: pending -> processing (atomic)
        doc = col.find_one_and_update({"_id": token, "status": "pending"}, {"$set": {"status": "processing"}})
        if not doc:
            send(chat_id, "This confirmation was already used or no longer exists. Send /buy again.")
            return
        if time.time() - doc["created"] > CONFIRM_SECONDS:
            col.update_one({"_id": token}, {"$set": {"status": "expired"}})
            send(chat_id, "Expired. Send /buy again.")
            return
        stuck = col.count_documents({"status": "processing", "_id": {"$ne": token},
                                     "created": {"$gt": time.time() - 900}})
        if stuck or not BUY_LOCK.acquire(blocking=False):
            col.update_one({"_id": token}, {"$set": {"status": "busy"}})
            send(chat_id, "Another order is processing (or ended unclear). Check Smile One orders first.")
            return
        try:
            s, before, smsg = live_session()
            if not s:
                col.update_one({"_id": token}, {"$set": {"status": "aborted", "note": smsg}})
                send(chat_id, smsg + "\n(No money spent)")
                return
            if not BUY_ENABLED:
                col.update_one({"_id": token}, {"$set": {"status": "dryrun"}})
                send(chat_id, "🧪 DRY-RUN finished. Player check, price and balance are fine. Nothing was bought.\n"
                              "Set BUY_ENABLED=1 on Render for a real purchase.")
                return
            send(chat_id, "Processing...")
            try:
                ok, info = sc.purchase(s, doc["user_id"], doc["zone_id"], doc["pid"])
            except sc.SmileError as e:
                col.update_one({"_id": token}, {"$set": {"status": "aborted", "note": str(e)}})
                send(chat_id, f"🛑 Stopped: {e}\n(Check /balance to be sure no money left)")
                return
            time.sleep(3)
            try:
                after = sc.get_balance(s)
            except Exception:
                after = "?"
            col.update_one({"_id": token}, {"$set": {"status": "paid" if ok else "unknown", "note": info,
                                                     "before": before, "after": after}})
            if ok:
                try:
                    charged = f"\nCharged: {round(before - after, 2)} Smile Coins"
                except Exception:
                    charged = ""
                send(chat_id, f"✅ Payment accepted ({info}).\nBalance: {before} -> {after}{charged}\n\n"
                              "This does NOT prove delivery. Check the game and "
                              "smile.one/customer/order (status Sucesso).")
            else:
                send(chat_id, f"⚠️ Not confirmed: {info}\nBalance: {before} -> {after}\n"
                              "Check smile.one/customer/order BEFORE trying again. Do not retry blindly.")
        finally:
            BUY_LOCK.release()
    except Exception:
        traceback.print_exc()
        if chat_id:
            send(chat_id, "⚠️ Unexpected error. The order may have gone through. Check /balance and "
                          "smile.one/customer/order. Do not retry before checking.")


def cmd_orders(chat_id, from_id):
    if not is_admin(from_id):
        send(chat_id, "Admin only.")
        return
    rows = list(orders_col().find({"status": {"$ne": "pending"}}).sort("created", -1).limit(8))
    if not rows:
        send(chat_id, "No orders recorded by this bot yet.")
        return
    lines = []
    for r in rows:
        t = time.strftime("%m-%d %H:%M", time.gmtime(r["created"]))
        lines.append(f"{t} UTC | pkg {r['pid']} | {r['status']} | {mask(r['user_id'])}")
    send(chat_id, "Last orders (this bot only):\n\n" + "\n".join(lines))


def handle_document(chat_id, from_id, msg):
    if not is_admin(from_id):
        send(chat_id, "Only the admin can send files.")
        return
    doc = msg["document"]
    name = (doc.get("file_name") or "").lower()
    if not name.endswith(".json") or doc.get("file_size", 0) > 1_000_000:
        send(chat_id, "Send smileone_session.json (a .json file under 1MB).")
        return
    try:
        info = requests.get(f"{API}/getFile", params={"file_id": doc["file_id"]}, timeout=20).json()
        raw = requests.get(f"https://api.telegram.org/file/bot{TOKEN}/{info['result']['file_path']}", timeout=30).content
        state = json.loads(raw)
        cookies = [c for c in state.get("cookies", []) if "smile.one" in (c.get("domain") or "")]
        if not cookies:
            send(chat_id, "No smile.one cookies found in that file.")
            return
        save_session(cookies)
        requests.post(f"{API}/deleteMessage", json={"chat_id": chat_id, "message_id": msg["message_id"]}, timeout=20)
        send(chat_id, f"Session saved ({len(cookies)} cookies). The file was deleted from the chat. Checking...")
        send(chat_id, check_session_text())
    except Exception:
        traceback.print_exc()
        send(chat_id, "Could not save the session. Check Render Logs (MONGODB_URI, file format).")


def handle(chat_id, from_id, text):
    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower()
    arg = arg.strip()
    try:
        if cmd == "/start":
            send(chat_id, "Smile One MLBB bot\n/myid\n/session_check (admin)\n/balance (admin)\n"
                          "/packages (admin)\n/buy <pkg> <UserID> <ZoneID> (admin)\n/orders (admin)")
        elif cmd == "/myid":
            send(chat_id, f"Your Telegram ID: {from_id}")
        elif not is_admin(from_id):
            send(chat_id, "Admin only.")
        elif cmd == "/session_check":
            send(chat_id, check_session_text())
        elif cmd == "/balance":
            s, bal, msg = live_session()
            send(chat_id, f"Smile Coins: {bal}" if s else msg)
        elif cmd == "/packages":
            send(chat_id, packages_text())
        elif cmd == "/buy":
            cmd_buy(chat_id, from_id, arg)
        elif cmd == "/orders":
            cmd_orders(chat_id, from_id)
    except sc.SmileError as e:
        send(chat_id, f"Error: {e}")
    except Exception:
        traceback.print_exc()
        send(chat_id, "Something failed. Check Render Logs. If you were buying, check smile.one/customer/order first.")


class Handler(BaseHTTPRequestHandler):
    def _reply(self, code, body=b"ok"):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._reply(200)

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def do_POST(self):
        if self.path != "/webhook" or self.headers.get("X-Telegram-Bot-Api-Secret-Token") != SECRET:
            self._reply(403, b"forbidden")
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            update = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._reply(400, b"bad request")
            return
        self._reply(200)
        msg = update.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat_id = (msg.get("chat") or {}).get("id")
        from_id = (msg.get("from") or {}).get("id")
        if update.get("callback_query"):
            threading.Thread(target=handle_callback, args=(update["callback_query"],), daemon=True).start()
        elif chat_id and msg.get("document"):
            threading.Thread(target=handle_document, args=(chat_id, from_id, msg), daemon=True).start()
        elif chat_id and text.startswith("/"):
            threading.Thread(target=handle, args=(chat_id, from_id, text), daemon=True).start()

    def log_message(self, *args):
        pass


def set_webhook():
    if not BASE_URL:
        print("RENDER_EXTERNAL_URL not found, webhook not set")
        return
    r = requests.post(f"{API}/setWebhook", timeout=20, json={
        "url": f"{BASE_URL}/webhook", "secret_token": SECRET,
        "allowed_updates": ["message", "callback_query"]})
    print("setWebhook:", r.status_code, r.text)


if __name__ == "__main__":
    set_webhook()
    threading.Thread(target=session_watch_loop, daemon=True).start()
    print(f"Listening on port {PORT} | BUY_ENABLED={BUY_ENABLED}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
