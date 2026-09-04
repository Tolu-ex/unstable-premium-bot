#!/usr/bin/env bash
# Finds the chat id of whoever has messaged your bot and writes it into .env.
# Send your bot a message first, then run this.
set -euo pipefail
cd "$(dirname "$0")"
set -a; . ./.env; set +a

/usr/bin/python3 - "$TELEGRAM_BOT_TOKEN" <<'PYEOF'
import json, re, sys, urllib.request

token = sys.argv[1]
url = f"https://api.telegram.org/bot{token}/getUpdates"
with urllib.request.urlopen(url, timeout=20) as resp:
    data = json.load(resp)

if not data.get("ok"):
    sys.exit("Telegram rejected the token: %s" % data.get("description"))

chats = {}
for update in data["result"]:
    chat = (update.get("message") or {}).get("chat") or {}
    if chat.get("id"):
        chats[chat["id"]] = (
            chat.get("username") or chat.get("first_name") or chat.get("title") or ""
        )

if not chats:
    sys.exit(
        "No messages yet.\n"
        "Open Telegram, find your bot, press Start or send it any message,\n"
        "then run this again."
    )

if len(chats) > 1:
    print("More than one chat has messaged this bot:")
    for cid, who in chats.items():
        print(f"  {cid}  ({who})")
    sys.exit("Put the right one in .env as TELEGRAM_CHAT_ID.")

chat_id, who = next(iter(chats.items()))
env = open(".env").read()
env = re.sub(r"^TELEGRAM_CHAT_ID=.*$", f"TELEGRAM_CHAT_ID={chat_id}", env, flags=re.M)
open(".env", "w").write(env)
print(f"Found chat {chat_id} ({who}) and wrote it to .env.")
print("You're ready: ./run.sh")
PYEOF
