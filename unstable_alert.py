#!/usr/bin/env python3
"""
Telegram alerter for the Unstable OTC desk (unstabletrade.com).

Watches the keeper offer books and pings you when a *fillable* offer appears
at or below your premium threshold.

Zero dependencies - Python 3.8+ stdlib only.
"""

import json
import math
import os
import ssl
import sys
import time
import html
import urllib.error
import urllib.parse
import urllib.request

# --- markets -----------------------------------------------------------------
# Both sides of the desk. Keys are what you pass to MARKETS in the env.
MARKETS = {
    "buy": {
        "label": "Buy USDC on Arc",
        "detail": "you pay on Ethereum → you get USDC on Arc",
        "api": "https://keeper.unstablebot.net/offers",
    },
    "sell": {
        "label": "Sell USDC to Ethereum",
        "detail": "you pay on Arc → you get USDC on Ethereum",
        "api": "https://keeper-sell-production.up.railway.app/offers",
    },
}

SERVICE_FEE = 0.03  # flat 3%, per the desk's docs
STATE_PATH = os.path.expanduser(
    os.environ.get("STATE_FILE", "~/.unstable-premium-bot-state.json")
)


# --- config ------------------------------------------------------------------
def env_str(key, default=None, required=False):
    val = os.environ.get(key, default)
    if required and not val:
        sys.exit(f"Missing required environment variable {key}. See README.md.")
    return val


def env_num(key, default):
    raw = os.environ.get(key)
    if raw is None:
        return default
    # systemd's EnvironmentFile keeps inline comments that a shell would drop.
    raw = raw.split("#", 1)[0].strip()
    if raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        sys.exit(f"{key} must be a number, got {raw!r}")


BOT_TOKEN = env_str("TELEGRAM_BOT_TOKEN", required=True)
# One or more destinations: a private chat, a group, or both.
CHAT_IDS = [c.strip() for c in env_str("TELEGRAM_CHAT_ID", required=True).split(",") if c.strip()]
if not CHAT_IDS:
    sys.exit("TELEGRAM_CHAT_ID is empty. See README.md.")

DEFAULTS = {
    # Alert when premium is at or below this, in percent.
    "max_premium": env_num("MAX_PREMIUM_PCT", 20.0),
    # Ignore dust. The site itself hides offers under 1 USDC; below this size
    # an offer is not worth a notification.
    "min_size": env_num("MIN_SIZE_USDC", 10.0),
    "muted": False,
}

POLL_SECONDS = int(env_num("POLL_SECONDS", 60))
# Don't nag about the same offer more often than this.
REALERT_HOURS = env_num("REALERT_HOURS", 6)
# Forget an offer we've alerted on once it's been gone from the book this long,
# so it can alert again if the maker re-posts it.
FORGET_MINUTES = env_num("FORGET_MINUTES", 60)

ENABLED_MARKETS = [
    m.strip().lower()
    for m in env_str("MARKETS", "buy").split(",")
    if m.strip()
]
for m in ENABLED_MARKETS:
    if m not in MARKETS:
        sys.exit(f"Unknown market {m!r}. Valid: {', '.join(MARKETS)}")


# --- state -------------------------------------------------------------------
def load_state():
    try:
        with open(STATE_PATH) as fh:
            state = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    state.setdefault("seen", {})           # offerId -> {premium_bps, alerted_at, last_seen}
    state.setdefault("settings", {})
    state.setdefault("update_offset", 0)
    # Settings can come from two places: .env, and /premium /size at runtime.
    # Runtime changes should survive a restart, but editing .env should still
    # take effect - so adopt an env value only when the env file itself has
    # changed since last boot.
    baseline = state.setdefault("env_baseline", {})
    changed = []
    for key, val in DEFAULTS.items():
        if key not in state["settings"]:
            state["settings"][key] = val
        elif baseline.get(key) != val:
            if state["settings"][key] != val:
                changed.append((key, state["settings"][key], val))
            state["settings"][key] = val
        baseline[key] = val
    state["_env_changes"] = changed
    return state


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    persisted = {k: v for k, v in state.items() if not k.startswith("_")}
    with open(tmp, "w") as fh:
        json.dump(persisted, fh, indent=2)
    os.replace(tmp, STATE_PATH)


# --- http --------------------------------------------------------------------
def build_ssl_context():
    """python.org builds on macOS ship without root certificates; prefer
    certifi's bundle when it happens to be installed."""
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


SSL_CONTEXT = build_ssl_context()
CERT_HELP = (
    "TLS certificate verification failed. This Python has no root "
    "certificates installed.\n"
    "Fix it with either:\n"
    "  open '/Applications/Python 3.11/Install Certificates.command'\n"
    "  python3 -m pip install certifi\n"
    "or run the bot with Apple's Python: /usr/bin/python3"
)


def urlopen(req, timeout):
    try:
        return urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT)
    except urllib.error.URLError as exc:
        if isinstance(getattr(exc, "reason", None), ssl.SSLCertVerificationError):
            raise RuntimeError(CERT_HELP) from exc
        raise


def get_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "unstable-premium-bot/1.0"})
    with urlopen(req, timeout) as resp:
        return json.loads(resp.read().decode())


def telegram(method, params, timeout=30):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data)
    try:
        with urlopen(req, timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        log(f"telegram {method} failed: {exc.code} {body}")
    except Exception as exc:  # noqa: BLE001 - never let the loop die on comms
        log(f"telegram {method} failed: {exc}")
    return None


def send_to(chat_id, text, silent=False):
    return telegram(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
            "disable_notification": "true" if silent else "false",
        },
    )


def send(text, silent=False):
    """Fan out to every configured chat. Truthy if at least one landed."""
    delivered = 0
    for chat_id in CHAT_IDS:
        if send_to(chat_id, text, silent):
            delivered += 1
        else:
            log(f"could not deliver to chat {chat_id}")
    return delivered


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


# --- offer maths -------------------------------------------------------------
def premium_pct(offer):
    """premiumBps is hundredths of a percent: 1900 -> 19.0%"""
    return offer["premiumBps"] / 100.0


def all_in(pct):
    """What you actually pay per 1 USDC received, matching the site's display."""
    return math.ceil((1 + pct / 100.0) * (1 + SERVICE_FEE) * 100) / 100


def fmt_pct(pct):
    return f"{pct:g}%"


def fmt_usdc(value):
    return f"{float(value):,.2f}"


def fetch_market(key):
    payload = get_json(MARKETS[key]["api"])
    offers = payload.get("offers", [])
    for offer in offers:
        offer["_market"] = key
    return offers


# --- alerting ----------------------------------------------------------------
def qualifying(offers, settings):
    hits = [
        o
        for o in offers
        if premium_pct(o) <= settings["max_premium"]
        and float(o["remainingUsdc"]) >= settings["min_size"]
    ]
    hits.sort(key=lambda o: (premium_pct(o), -float(o["remainingUsdc"])))
    return hits


def should_alert(offer, state, now):
    record = state["seen"].get(offer["offerId"])
    if record is None:
        return True
    # A maker cutting their price is news even if we've alerted on this offer.
    if offer["premiumBps"] < record["premium_bps"]:
        return True
    if now - record["alerted_at"] > REALERT_HOURS * 3600:
        return True
    return False


def format_alert(hits, settings):
    lines = [
        f"\U0001f6a8 <b>Unstable premium below {fmt_pct(settings['max_premium'])}</b>",
        "",
    ]
    for offer in hits:
        pct = premium_pct(offer)
        market = MARKETS[offer["_market"]]
        maker = offer["seller"]
        lines.append(
            f"<b>{fmt_pct(pct)}</b> premium · {all_in(pct):.2f}× all-in\n"
            f"   {fmt_usdc(offer['remainingUsdc'])} USDC available"
            f" · {html.escape(market['label'])}\n"
            f"   maker <code>{html.escape(maker[:6])}…{html.escape(maker[-4:])}</code>"
        )
    lines.append("")
    lines.append(
        f"<i>min size {fmt_usdc(settings['min_size'])} USDC</i> · "
        "https://www.unstabletrade.com/"
    )
    return "\n".join(lines)


def check_offers(state):
    settings = state["settings"]
    now = time.time()

    offers = []
    for key in ENABLED_MARKETS:
        try:
            offers.extend(fetch_market(key))
        except Exception as exc:  # noqa: BLE001
            log(f"could not fetch {key} book: {exc}")
            return  # try again next tick rather than acting on a partial book

    live_ids = {o["offerId"] for o in offers}
    hits = qualifying(offers, settings)

    fresh = [o for o in hits if should_alert(o, state, now)]

    if fresh:
        if settings["muted"]:
            # Deliberately don't record these as alerted: once unmuted, they
            # are still news.
            log(f"{len(fresh)} qualifying offer(s) suppressed (muted)")
        elif send(format_alert(fresh, settings)):
            log(f"alerted on {len(fresh)} offer(s)")
            for offer in fresh:
                state["seen"][offer["offerId"]] = {
                    "premium_bps": offer["premiumBps"],
                    "alerted_at": now,
                    "last_seen": now,
                }
        else:
            # Telegram was unreachable. Leave the offers unrecorded so the
            # alert goes out on the next tick instead of being lost.
            log(f"could not deliver alert for {len(fresh)} offer(s); will retry")
    for offer in offers:
        record = state["seen"].get(offer["offerId"])
        if record:
            record["last_seen"] = now

    # Forget offers that have been off the book long enough to count as new
    # if the maker posts them again.
    cutoff = now - FORGET_MINUTES * 60
    state["seen"] = {
        oid: rec
        for oid, rec in state["seen"].items()
        if oid in live_ids or rec["last_seen"] > cutoff
    }

    cheapest = hits[0] if hits else None
    if cheapest:
        log(
            f"book: {len(offers)} offers, {len(hits)} qualifying, "
            f"best {fmt_pct(premium_pct(cheapest))}"
        )
    else:
        tradeable = [
            o for o in offers if float(o["remainingUsdc"]) >= settings["min_size"]
        ]
        best = min((premium_pct(o) for o in tradeable), default=None)
        log(
            f"book: {len(offers)} offers, none under "
            f"{fmt_pct(settings['max_premium'])}"
            + (f" (best fillable {fmt_pct(best)})" if best is not None else "")
        )
    save_state(state)


# --- commands ----------------------------------------------------------------
def cmd_status(state, _arg):
    settings = state["settings"]
    markets = ", ".join(MARKETS[m]["label"] for m in ENABLED_MARKETS)
    alert_state = "\U0001f507 muted" if settings["muted"] else "\U0001f514 on"
    return (
        "<b>Watching</b>\n"
        f"Markets: {html.escape(markets)}\n"
        f"Alert at or below: <b>{fmt_pct(settings['max_premium'])}</b> premium\n"
        f"Minimum size: <b>{fmt_usdc(settings['min_size'])} USDC</b>\n"
        f"Poll: every {POLL_SECONDS}s\n"
        f"Alerts: {alert_state}\n\n"
        "/book – cheapest fillable offers now\n"
        "/premium 15 – change the threshold\n"
        "/size 50 – change the minimum size\n"
        "/mute /unmute"
    )


def cmd_book(state, _arg):
    settings = state["settings"]
    rows = []
    for key in ENABLED_MARKETS:
        try:
            offers = fetch_market(key)
        except Exception as exc:  # noqa: BLE001
            return f"Could not reach the {MARKETS[key]['label']} book: {html.escape(str(exc))}"
        tradeable = [
            o for o in offers if float(o["remainingUsdc"]) >= settings["min_size"]
        ]
        tradeable.sort(key=lambda o: premium_pct(o))
        rows.append((key, tradeable[:5], len(offers)))

    out = []
    for key, top, total in rows:
        out.append(f"<b>{html.escape(MARKETS[key]['label'])}</b>")
        if not top:
            out.append(
                f"  nothing at or above {fmt_usdc(settings['min_size'])} USDC"
                f" ({total} offers total, mostly dust)"
            )
        for offer in top:
            pct = premium_pct(offer)
            out.append(
                f"  {fmt_pct(pct)} · {all_in(pct):.2f}× · "
                f"{fmt_usdc(offer['remainingUsdc'])} USDC"
            )
        out.append("")
    out.append(f"<i>offers under {fmt_usdc(settings['min_size'])} USDC hidden</i>")
    return "\n".join(out)


def cmd_premium(state, arg):
    try:
        value = float(arg.strip().rstrip("%"))
    except ValueError:
        return "Usage: <code>/premium 15</code> — alert when premium is 15% or lower."
    if not 0 < value <= 10000:
        return "Premium threshold must be between 0 and 10000."
    state["settings"]["max_premium"] = value
    # Clear the alert history so the new threshold gets a clean first pass.
    state["seen"] = {}
    save_state(state)
    return f"Now alerting at or below <b>{fmt_pct(value)}</b> premium."


def cmd_size(state, arg):
    try:
        value = float(arg.strip())
    except ValueError:
        return "Usage: <code>/size 50</code> — ignore offers smaller than 50 USDC."
    if value < 0:
        return "Minimum size cannot be negative."
    state["settings"]["min_size"] = value
    state["seen"] = {}
    save_state(state)
    return f"Ignoring offers under <b>{fmt_usdc(value)} USDC</b>."


def cmd_mute(state, _arg):
    state["settings"]["muted"] = True
    save_state(state)
    return "\U0001f507 Muted. I'll keep watching but stay quiet. /unmute to resume."


def cmd_unmute(state, _arg):
    state["settings"]["muted"] = False
    state["seen"] = {}
    save_state(state)
    return "\U0001f514 Alerts back on."


COMMANDS = {
    "start": cmd_status,
    "status": cmd_status,
    "book": cmd_book,
    "premium": cmd_premium,
    "size": cmd_size,
    "mute": cmd_mute,
    "unmute": cmd_unmute,
}


_announced_unknown = set()


def handle_updates(state, timeout):
    """Long-poll for commands. Doubles as the loop's sleep."""
    result = telegram(
        "getUpdates",
        {
            "offset": state["update_offset"],
            "timeout": timeout,
            "allowed_updates": json.dumps(["message"]),
        },
        timeout=timeout + 15,
    )
    if not result or not result.get("ok"):
        # getUpdates failed; sleep so we don't spin.
        if timeout:
            time.sleep(min(timeout, 15))
        return 0

    for update in result["result"]:
        state["update_offset"] = update["update_id"] + 1
        message = update.get("message") or {}
        # Only take orders from a configured chat.
        chat = message.get("chat") or {}
        origin = str(chat.get("id"))
        if origin not in CHAT_IDS:
            # Say so once per chat, otherwise a chat we've never heard of is
            # dropped in complete silence and is impossible to diagnose.
            if origin not in _announced_unknown:
                _announced_unknown.add(origin)
                name = chat.get("title") or chat.get("first_name") or "?"
                log(
                    f"ignoring message from unconfigured chat {origin} "
                    f"({chat.get('type')}, {name!r}) - add it to TELEGRAM_CHAT_ID to enable"
                )
            continue
        text = (message.get("text") or "").strip()
        if not text.startswith("/"):
            continue
        head, _, arg = text.partition(" ")
        name = head[1:].split("@")[0].lower()
        handler = COMMANDS.get(name)
        if handler:
            try:
                send_to(origin, handler(state, arg))
            except Exception as exc:  # noqa: BLE001
                log(f"command /{name} failed: {exc}")
                send_to(origin, "Something went wrong running that command.")
        else:
            send_to(origin, "Unknown command. Try /status")
    save_state(state)
    return len(result["result"])


# --- main --------------------------------------------------------------------
KEEPALIVE_DAYS = 20


def run_test():
    """Prove the whole chain works: network, token, delivery.

    Sends to the FIRST configured chat only, so testing never spams a group.
    """
    who = telegram("getMe", {})
    if not who or not who.get("ok"):
        sys.exit("Could not authenticate with Telegram - check the bot token.")
    log(f"authenticated as @{who['result']['username']}")

    try:
        offers = fetch_market(ENABLED_MARKETS[0])
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"Could not reach the offer book: {exc}")
    log(f"offer book reachable: {len(offers)} offers")

    target = CHAT_IDS[0]
    if not send_to(target, "\U0001f9ea Test from GitHub Actions - the watcher can reach you."):
        sys.exit(f"Could not deliver a message to chat {target}.")
    log(f"test message delivered to chat {target}")


def run_once():
    """One pass, then exit: answer queued commands, check the book, save.

    Used by the GitHub Actions schedule, where nothing can stay resident.
    """
    state = load_state()
    for key, was, now_ in state.get("_env_changes", []):
        log(f".env changed {key}: {was} -> {now_}")
    if state.get("_env_changes"):
        state["seen"] = {}

    # Drain whatever arrived since the last run. timeout=0 returns immediately.
    handled = 0
    for _ in range(10):
        count = handle_updates(state, 0)
        handled += count
        if not count:
            break
    if handled:
        log(f"answered {handled} queued message(s)")

    check_offers(state)

    # GitHub disables cron on repos with no commits for 60 days. Nudging this
    # timestamp every few weeks guarantees the state file changes, which gives
    # the workflow something to commit.
    now = time.time()
    if now - state.get("last_keepalive", 0) > KEEPALIVE_DAYS * 86400:
        state["last_keepalive"] = now
        log("keepalive touched")

    save_state(state)


def main():
    state = load_state()
    settings = state["settings"]
    for key, was, now_ in state.get("_env_changes", []):
        log(f".env changed {key}: {was} -> {now_}")
    if state.get("_env_changes"):
        # New thresholds deserve a clean first pass.
        state["seen"] = {}
    markets = ", ".join(MARKETS[m]["label"] for m in ENABLED_MARKETS)
    log(
        f"watching {markets} | premium <= {settings['max_premium']}% "
        f"| size >= {settings['min_size']} USDC | every {POLL_SECONDS}s"
    )

    if os.environ.get("ANNOUNCE_START", "1").split("#", 1)[0].strip() != "0":
        send(
            "✅ <b>Unstable watcher started</b>\n"
            f"Alerting when premium drops to <b>{fmt_pct(settings['max_premium'])}</b>"
            f" or lower on at least <b>{fmt_usdc(settings['min_size'])} USDC</b>.\n\n"
            "/status for settings, /book for the current top of book.",
            silent=True,
        )

    next_check = 0.0
    while True:
        now = time.time()
        if now >= next_check:
            try:
                check_offers(state)
            except Exception as exc:  # noqa: BLE001
                log(f"check failed: {exc}")
            next_check = time.time() + POLL_SECONDS

        wait = max(1, min(25, int(next_check - time.time())))
        handle_updates(state, wait)


if __name__ == "__main__":
    try:
        if "--test" in sys.argv:
            run_test()
        elif "--once" in sys.argv:
            run_once()
        else:
            main()
    except KeyboardInterrupt:
        log("stopped")
