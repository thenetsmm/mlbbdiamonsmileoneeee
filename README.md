# Smile One MLBB Telegram bot (Render, webhook mode)

Telegram bot (admin only) that buys Mobile Legends diamonds on Smile One (Brazil)
with Smile Coins. Login uses a session exported from your own Chrome (Google login).

## Files
- bot_render.py  : Telegram webhook bot, session watcher, /buy flow
- smile_client.py: Smile One logic (balance, player check, purchase)
- tools/save_session_smile.py : laptop only, exports smileone_session.json

## Render
- Build Command : pip install -r requirements.txt
- Start Command : python bot_render.py
- Instance type : Free

## Environment variables
| Key | Value |
|---|---|
| BOT_TOKEN | token from BotFather |
| WEBHOOK_SECRET | random string (letters, digits, - _) |
| ADMIN_ID | your Telegram user ID (/myid shows it) |
| MONGODB_URI | MongoDB connection string |
| BUY_ENABLED | 0 = DRY-RUN (default), 1 = real purchase |
| MAX_ORDER_COINS | max Smile Coins per order (default 70) |
| MAX_PER_DAY | max purchases per 24h (default 3) |

## Commands
/start /myid (everyone) - /session_check /balance /packages /orders /buy 13 <UserID> <ZoneID> (admin)

## Session steps (laptop)
1. Close all Chrome, then start it with a debug port:
   & "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="D:\smileone\chrome-profile"
2. In that Chrome open https://www.smile.one/br/ and log in with Google yourself.
3. In another PowerShell: python -m pip install playwright ; python tools/save_session_smile.py
4. Send the smileone_session.json file to the bot chat (the bot stores it in MongoDB and deletes the file from the chat).
5. Close that Chrome (an open debug port is not safe).
6. Send /session_check. If it says the site rejects the session, Render's IP is not accepted
   and the bot cannot run from Render.

## First test order
1. BUY_ENABLED=0: /buy 13 <UserID> <ZoneID>, confirm -> DRY-RUN, nothing is bought.
2. BUY_ENABLED=1: buy once, then check the game and smile.one/customer/order.

Never commit tokens, MONGODB_URI or smileone_session.json to GitHub.
