# Unstable premium alerter

A Telegram bot that watches the [Unstable](https://www.unstabletrade.com/) OTC
desk and pings you when a **fillable** offer appears at or below your premium
threshold (default 20%).

No dependencies — Python 3.8+ standard library only.

## The thing to know before you start

The offer book is full of dust. At the time of writing there were offers at
**0%, 5%, 10%, 12.5%, 17% and 19% premium** — every one of them under 1 USDC,
several under 0.2 USDC. The cheapest offer you could actually trade was **65%**.

The website hides anything under 1 USDC, which is why its book appears to start
in the 60s. A naive "alert me under 20%" would fire constantly and never once
be actionable.

So the bot filters on size as well as premium. `MIN_SIZE_USDC` defaults to 10.
Set it to roughly the size you'd actually want to fill.

## Setup

**1. Create the bot.** In Telegram, message [@BotFather](https://t.me/BotFather),
send `/newbot`, follow the prompts. It gives you a token like
`123456:ABC-DEF...`. Keep it secret — anyone with it controls the bot.

**2. Configure.**

```bash
cd ~/unstable-premium-bot
cp .env.example .env
```

Put your token in `.env` as `TELEGRAM_BOT_TOKEN`.

`TELEGRAM_CHAT_ID` accepts more than one destination, comma-separated — a
private chat and a group, for example:

```
TELEGRAM_CHAT_ID=123456789,-1001234567890
```

Alerts go to all of them. Commands are accepted from any of them, and the reply
goes back to whichever chat asked. To add the bot to a group: add it as a
member, send `/start` in the group, then run `./get_chat_id.sh` — group ids are
negative.

**3. Get your chat id.** Open your bot in Telegram, press **Start** or send it
any message, then:

```bash
./get_chat_id.sh
```

It finds the chat and writes `TELEGRAM_CHAT_ID` into `.env` for you.

**4. Run it.**

```bash
./run.sh
```

You should get a "watcher started" message in Telegram.

## Commands

Message the bot:

| Command | What it does |
|---|---|
| `/status` | Current threshold, size filter and mute state |
| `/book` | The cheapest fillable offers right now |
| `/premium 15` | Alert at 15% or lower instead |
| `/size 50` | Ignore offers smaller than 50 USDC |
| `/mute` / `/unmute` | Pause and resume alerts |

The bot ignores commands from any chat other than `TELEGRAM_CHAT_ID`.

Settings changed with `/premium` and `/size` persist across restarts. Editing
`.env` also takes effect on the next restart, and wins over a command-set value
when the file itself has changed — so `.env` is your baseline and the commands
are for quick adjustments.

## When it alerts

An offer fires when its premium is at or below your threshold **and** it's at
least `MIN_SIZE_USDC`. After that the bot stays quiet about it unless:

- the maker **cuts the price further**, or
- `REALERT_HOURS` (6h) pass and it's still there, or
- it leaves the book for `FORGET_MINUTES` (60m) and is reposted.

Multiple qualifying offers in one poll arrive as a single message, not a burst.
If Telegram is unreachable the alert isn't marked as sent — it goes out on the
next poll rather than being lost.

## Keeping it running

`./run.sh` only alerts while it's running, so on a laptop it's asleep when you
are. For always-on, either leave it in a terminal / `tmux`, or install it as a
launch agent that restarts itself and survives reboots:

```bash
./install_service.sh      # start on login, restart if it dies
launchctl unload ~/Library/LaunchAgents/com.unstable.premiumbot.plist   # to stop
```

Logs go to `~/Library/Logs/unstable-premiumbot.log`.

**The project must not live in `~/Desktop`, `~/Documents` or `~/Downloads`.**
macOS protects those folders, and a launchd agent has no permission to read
them — the service fails with `Operation not permitted` before it ever starts.
`~/unstable-premium-bot` (used here) is fine.

### Running on GitHub Actions (how this one is deployed)

`.github/workflows/watch.yml` runs `unstable_alert.py --once` on a 5-minute
cron. Nothing stays resident: each run answers any queued commands, checks the
book, alerts if warranted, and commits `state.json` back to the repo so the
next run knows what it has already alerted on.

- **Settings** live as plain `env:` values in the workflow. Edit, commit, push.
- **Secrets** (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) are GitHub Actions
  secrets, never in the repo. Rotate with
  `gh secret set TELEGRAM_BOT_TOKEN --repo <owner>/<repo>`.
- **Commands still work**, but replies take until the next run — up to ~5
  minutes, not instant.
- **Verify the pipeline** any time from the Actions tab: run the workflow
  manually with `self_test` ticked. It authenticates, fetches the book, and
  messages the *first* configured chat only, so testing never spams a group.

Caveats worth knowing:

- GitHub runs cron on a best-effort basis. Under load a 5-minute schedule can
  slip by several minutes. It is not a low-latency trading signal.
- Scheduled workflows are disabled automatically after **60 days without
  commits**. The bot nudges a `last_keepalive` timestamp every 20 days so the
  state commit keeps the repo active.
- Public repos get unlimited free Actions minutes. On a private repo the free
  tier is 2000 min/month and each run bills rounded up to a whole minute, so a
  5-minute cron would blow through it.

### What "always on" actually means

The launch agent restarts the bot if it crashes and starts it again at login,
but it cannot run while the Mac is **asleep or shut down**. A closed laptop
misses alerts.

For real 24/7, put it on something that never sleeps — a cheap VPS, a Raspberry
Pi, or a free Railway/Fly container. It's one file with no dependencies and no
database, so deploying is: copy the folder, set the same environment variables,
run `python3 unstable_alert.py`. Run it in exactly one place at a time, or both
copies will fight over the command queue and you'll get duplicate alerts.
(That is why the macOS launch agent is uninstalled when running on Actions.)

## Where the data comes from

The site's own frontend polls these every 15s; the bot uses the same endpoints
at 60s:

- Buy on Arc — `https://keeper.unstablebot.net/offers`
- Sell to Ethereum — `https://keeper-sell-production.up.railway.app/offers`

`premiumBps` is hundredths of a percent (1900 = 19%). The all-in multiplier the
bot reports is `ceil((1 + premium) × 1.03, 2)`, which matches the site's
displayed figure exactly (verified against 68%, 69%, 70%, 100%, 500%, 2000%).

These are undocumented internal endpoints. They can change without notice — if
alerts go quiet, check that they still return JSON.

## Troubleshooting

**`CERTIFICATE_VERIFY_FAILED`** — python.org's macOS build ships without root
certificates. Either run `open "/Applications/Python 3.11/Install
Certificates.command"`, or `pip install certifi`, or use Apple's Python
(`PYTHON=/usr/bin/python3`, which is what `run.sh` defaults to).

**Nothing replies to commands** — the bot only answers while the process is
running. Check with `pgrep -f unstable_alert.py`; if it's empty, start it with
`./run.sh` or `./install_service.sh`. Messages you sent while it was down are
queued and get answered when it next starts.

**No alerts ever** — likely correct. Run `/book` to see the real top of book;
premiums have been sitting in the 60s+. Confirm with `/status` that your
threshold and size filter are what you expect.

**Alerts for tiny offers** — raise `MIN_SIZE_USDC` or use `/size`.

## A caveat worth stating

This tells you a price exists; it doesn't tell you it's a good trade. A premium
far below the rest of the book is more likely to be bait, a stale offer, or
attached to a maker who won't settle than it is free money. The desk is
non-custodial and escrow-backed, but nothing here vets counterparties. Treat an
alert as a prompt to go look, not a signal to fill.
