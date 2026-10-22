"""PigeonLabs GPU Watcher — gpu_watcher.py

PUBLISHED (SANITISED) COPY of the owner's private, single-user eBay bargain
watcher. Everything secret is read from the environment at run time — this
file contains no App IDs, no Cert IDs, no OAuth tokens, no webhook URLs, no
personal data. See README.md for the project's purpose, flow and compliance
notes; the comments throughout document WHY each decision was made, for
human readers (and for eBay Developers Program reviewers evaluating the
integration), not just what the code does.

Zero-LLM design: this is a plain scheduled Python script. No inference, no
GPU, no browser, no scraping. One saved search, one private Discord channel.

Every tick:
  1. Get an eBay application token (client credentials, cached ~24h).
  2. Search the Browse API: category 27386 (Graphics/Video Cards),
     condition 7000 (For parts or not working), Buy It Now, item location
     GB, newest first.
  3. Diff item IDs against a local SQLite state DB; post genuinely new
     listings to the owner's Discord webhook (batched embeds).
  4. The very first successful run seeds a silent baseline so the channel
     is not flooded with everything currently listed.

Failure policy: persistent faults alert via Discord at most once per 24h
per distinct fault (fingerprint dedup), then exit 0 so the scheduler stays
quiet. Unhandled crashes exit non-zero so the scheduler records the failure.

Compliance posture (as implemented):
  * All eBay data access is through official eBay Developers Program REST
    APIs with OAuth application tokens. No scraping, no site automation.
  * Cadence is fixed and bounded: <= 3 search calls per 2-minute tick
    (<= ~2,160 calls/day) against a 5,000 calls/day Browse allocation.
  * No commerce flows, no user messaging, no PII about eBay users; alerts
    are personal-view only and link straight back to the eBay listing.

Config (environment, supplied via the scheduler's private .env):
  EBAY_APP_ID, EBAY_CERT_ID      production keyset (required)
  DISCORD_WEBHOOK_URL            webhook for alerts (required to notify)
  EBAY_WATCHER_Q                 search query, default: '"graphics card" | gpu'
  EBAY_WATCHER_SORT              sort, default: -listing_date
  EBAY_WATCHER_FIXTURE           TEST ONLY: JSON file of itemSummaries, skips network
"""

import datetime
import fcntl
import json
import logging
import logging.handlers
import os
import pathlib
import sqlite3
import sys
import time

import requests

# --- Paths -----------------------------------------------------------------
HERMES_HOME = pathlib.Path(os.environ.get("HERMES_HOME", "/opt/data"))
DATA_DIR = HERMES_HOME / "ebay-watcher"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "state.sqlite"
TOKEN_PATH = DATA_DIR / "app_token.json"
LOG_PATH = DATA_DIR / "watcher.log"

# --- Search configuration ----------------------------------------------------
TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
# --- Why the search looks like this (coverage notes for human readers) -----
# The Browse API is the modern REST index, and for this category it is
# structurally INCOMPLETE compared to what the eBay website shows:
#   * listings attached to the product catalogue (EPID product pages) often
#     do not surface in searchItems results at all, and
#   * many low-volume personal/small-seller listings are absent.
# Verified 2026-10-22 by direct comparison: several live, in-category,
# parts-condition listings visible on the eBay site were unreturnable via
# searchItems by item ID, seller, title, or price.
# The owner has requested Shopping API (Approval Level 2) access through an
# eBay Application Growth Check so alerts can match the site via official
# APIs; until that is granted this watcher alerts on the Browse-visible
# subset only. Deliberately NOT closed via scraping: staying on APIs.
SEARCH_PARAMS = {
    "q": os.environ.get("EBAY_WATCHER_Q", '"graphics card" | gpu'),
    "category_ids": "27386",         # Computers > Graphics/Video Cards
    # NOTE: eBay Browse silently IGNORES every condition-filter syntax
    # (conditionIds/conditionGroupIds/conditionFilter, verified 2026-10-22:
    # identical results with/without). Condition 7000 is enforced in code
    # (KEEP_CONDITION). buyingFormat + itemLocation ARE honoured (2475->924).
    "filter": "buyingFormat:FIXED_PRICE,itemLocationCountry:GB",
    "sort": os.environ.get("EBAY_WATCHER_SORT", "-listing_date"),
    "limit": "200",
}
PAGE_CAP = 3                 # max pages (600 items) scanned per tick:
                               # newest-first, so 3 pages clears normal UK
                               # GPU traffic many times over per 2-min tick
KEEP_CONDITION = "7000"      # eBay condition id: For parts or not working
MAX_ALERTS_PER_TICK = 12     # flood guard (learned the hard way on day one:
                               # a mass relist produced 600 first-scan items).
                               # Cap newest-first; the rest are marked seen and
                               # linked via the saved-search URL in the header.
ALERT_REFIRE_SECONDS = 24 * 3600   # re-alert a persistent fault at most once/day
EMBEDS_PER_MESSAGE = 8       # Discord allows 10 embeds/msg; keep headroom
HTTP_TIMEOUT = 30

log = logging.getLogger("ebay-watcher")


def setup_logging() -> None:
    log.setLevel(logging.INFO)
    fh = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    log.addHandler(fh)
    log.addHandler(sh)


def dotenv_fallback(key: str) -> str:
    """Scheduler injects .env values as env vars; parse HERMES_HOME/.env so
    manual runs from a shell behave identically. Never logs values."""
    val = os.environ.get(key, "").strip()
    if val:
        return val
    env_file = HERMES_HOME / ".env"
    try:
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


class WatcherError(Exception):
    """A fault that should be reported through the fault-alert dedup path."""


# --- State -------------------------------------------------------------------
# SQLite (WAL) keeps three things: every scanned itemId, a title+price
# fingerprint index, and a small meta table (baseline latch, fault latch).
# Nothing eBay-side is stored long-term beyond this local de-dup index.
def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS seen("
        "item_id TEXT PRIMARY KEY, title TEXT, price TEXT, "
        "listing_date TEXT, first_seen TEXT)"
    )
    conn.execute("CREATE TABLE IF NOT EXISTS seen_fp("
        "fp TEXT PRIMARY KEY, item_id TEXT, created TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
    return conn


def fingerprint(it: dict) -> str:
    """Title+price fingerprint: catches bulk relists (new itemId, same deal)."""
    norm = "".join(ch for ch in (it.get("title") or "").lower() if ch.isalnum())
    price = (it.get("price") or {}).get("value") or "?"
    return norm[:160] + "|" + str(price)


def mark_seen(conn, it: dict) -> None:
    iid = str(it.get("itemId", ""))
    if not iid:
        return
    now = datetime.datetime.now(datetime.UTC).isoformat()
    conn.execute("INSERT OR IGNORE INTO seen VALUES(?,?,?,?,?)",
                 (iid, (it.get("title") or "")[:250],
                  (it.get("price") or {}).get("value", ""),
                  it.get("listingDate", ""), now))
    conn.execute("INSERT OR IGNORE INTO seen_fp VALUES(?,?,?)",
                 (fingerprint(it), iid, now))


def meta_get(conn, key, default=""):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def meta_set(conn, key, value):
    conn.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
    conn.commit()


# --- eBay API ----------------------------------------------------------------
# Application (client-credentials) token ONLY: this integration has no user
# authorisation, stores no user data, and touches no scoped user endpoints.
def get_app_token() -> str:
    app_id = dotenv_fallback("EBAY_APP_ID")
    cert_id = dotenv_fallback("EBAY_CERT_ID")
    if not app_id or not cert_id:
        raise WatcherError("eBay credentials missing (EBAY_APP_ID/EBAY_CERT_ID in .env)")

    # Cache: reuse until 5 min before expiry.
    try:
        cached = json.loads(TOKEN_PATH.read_text())
        if cached.get("expires_at", 0) > time.time() + 300:
            return cached["token"]
    except (OSError, ValueError, KeyError):
        pass

    resp = requests.post(
        TOKEN_URL, auth=(app_id, cert_id),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials",
              "scope": "https://api.ebay.com/oauth/api_scope"},
        timeout=HTTP_TIMEOUT)
    if resp.status_code == 401:
        raise WatcherError(
            "eBay token endpoint rejected our keyset (401 invalid_client). "
            "Check the Production keyset is enabled in the eBay developer portal "
            "and that App ID + Cert ID are from the SAME Production keyset.")
    if resp.status_code != 200:
        raise WatcherError(f"eBay token request failed: HTTP {resp.status_code} "
                           f"{resp.text[:200]}")
    body = resp.json()
    token = body["access_token"]
    tmp = TOKEN_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"token": token,
                               "expires_at": time.time() + int(body.get("expires_in", 7200))}))
    os.chmod(TOKEN_PATH.with_suffix(".tmp"), 0o600)
    tmp.replace(TOKEN_PATH)
    return token


def search_page(token: str, offset: int) -> dict:
    params = dict(SEARCH_PARAMS, offset=str(offset))
    resp = requests.get(
        SEARCH_URL,
        headers={"Authorization": f"Bearer {token}",
                 "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB"},
        params=params, timeout=HTTP_TIMEOUT)
    if resp.status_code == 401:
        TOKEN_PATH.unlink(missing_ok=True)  # token revoked/expired early
        resp = requests.get(
            SEARCH_URL,
            headers={"Authorization": f"Bearer {get_app_token()}",
                     "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB"},
            params=params, timeout=HTTP_TIMEOUT)
    if resp.status_code in (429, 500, 502, 503):
        raise WatcherError(f"eBay search transient failure: HTTP {resp.status_code}")
    if resp.status_code != 200:
        raise WatcherError(f"eBay search failed: HTTP {resp.status_code} {resp.text[:300]}")
    return resp.json()


# --- Discord -----------------------------------------------------------------
def post_to_webhook(payload: dict) -> None:
    url = dotenv_fallback("DISCORD_WEBHOOK_URL")
    if not url:
        raise WatcherError("DISCORD_WEBHOOK_URL not set in .env — cannot deliver alerts")
    for attempt in (1, 2):
        try:
            resp = requests.post(url, json=payload, timeout=15)
            if resp.status_code in (200, 204):
                return
            if resp.status_code == 429:  # webhook rate limit: brief backoff, one retry
                time.sleep(float(resp.headers.get("Retry-After", 2)) + 0.5)
                continue
            raise WatcherError(f"Discord webhook HTTP {resp.status_code}: {resp.text[:200]}")
        except requests.ConnectionError as exc:
            if attempt == 2:
                raise WatcherError(f"Discord webhook unreachable: {exc}") from exc
            time.sleep(3)


def item_embed(it: dict) -> dict:
    price = (it.get("price") or {}).get("value", "?")
    ship = "not specified"
    so = (it.get("shippingOptions") or [{}])[0]
    sc = so.get("shippingCost") or {}
    if so.get("shippingCostType") == "FREE":
        ship = "free"
    elif sc.get("value"):
        ship = sc["value"]
    loc = (it.get("itemLocation") or {}).get("country", "?")
    fields = (f"🔧 **FOR PARTS OR NOT WORKING** — eBay condition "
              f"{it.get('conditionId')} verified\n"
              f"**£{price}** • postage: {ship} • located {loc}")
    return {"title": (it.get("title") or "?")[:250],
            "url": it.get("itemWebUrl", "https://www.ebay.co.uk/"),
            "description": fields[:1000],
            "color": 0xE67E22}


EBAY_BROWSE_URL = ("https://www.ebay.co.uk/sch/27386/i.html?LH_ItemCondition"
                   "=7000&LH_BIN=1&LH_PrefLoc=3&_sop=15")


def send_listing_alerts(items: list, overflow: int = 0) -> None:
    noun = "listing" if len(items) == 1 else "listings"
    for i in range(0, len(items), EMBEDS_PER_MESSAGE):
        chunk = items[i:i + EMBEDS_PER_MESSAGE]
        first = i == 0
        header = (f"🖥️ **New eBay UK GPU {noun}** — "
                  f"🔧 For parts/not working • Buy It Now • UK "
                  f"— {len(chunk)} new"
                  + (f" (batch {i // EMBEDS_PER_MESSAGE + 1})"
                     if len(items) > EMBEDS_PER_MESSAGE else ""))
        if first and overflow:
            header += (f"\n⚠️ {overflow} more new listing(s) scanned this tick "
                       f"(capped) — browse them: <{EBAY_BROWSE_URL}>")
        post_to_webhook({"content": header,
                         "embeds": [item_embed(it) for it in chunk]})


def alert_fault(conn: sqlite3.Connection, message: str) -> None:
    """Rate-limited fault visibility: alert on a NEW fault fingerprint, or when
    a persistent fault has been silent for >24h. Never spams every tick."""
    fp = message[:300]
    now = time.time()
    if meta_get(conn, "last_error_fp") == fp and \
       now - float(meta_get(conn, "last_error_alert_ts", "0")) < ALERT_REFIRE_SECONDS:
        log.warning("fault persists (already alerted): %s", fp)
        return
    meta_set(conn, "last_error_fp", fp)
    meta_set(conn, "last_error_alert_ts", int(now))
    try:
        post_to_webhook({"content": f"⚠️ **eBay GPU watcher fault**\n> {fp}\n"
                                    f"(re-alerts at most daily while it persists)"})
    except Exception as exc:  # last resort: cron docs + log still record it
        log.error("fault alert delivery failed: %s", exc)


# --- Main --------------------------------------------------------------------
def fetch_new_items(conn) -> tuple[list, int, int]:
    """Scan newest-first pages. New = unseen itemId AND condition 7000 AND
    title+price not seen before (relist guard). Non-matching items are marked
    seen too (they are scanned, not pending). Baseline marks everything."""
    token = get_app_token()
    baseline = meta_get(conn, "baseline_done") != "1"
    new_items, fetched, offset = [], 0, 0
    for _page in range(PAGE_CAP):
        data = search_page(token, offset)
        items = data.get("itemSummaries") or []
        fetched += len(items)
        unknown = 0
        for it in items:
            item_id = str(it.get("itemId", ""))
            if not item_id:
                continue
            if baseline:
                mark_seen(conn, it)
                continue
            if conn.execute("SELECT 1 FROM seen WHERE item_id=?", (item_id,)).fetchone():
                continue
            unknown += 1
            fp = fingerprint(it)
            if str(it.get("conditionId", "")) != KEEP_CONDITION:
                mark_seen(conn, it)          # scanned; not a parts listing
                continue
            if conn.execute("SELECT 1 FROM seen_fp WHERE fp=?", (fp,)).fetchone():
                mark_seen(conn, it)          # relist of an already-alerted deal
                continue
            new_items.append(it)
        conn.commit()
        if baseline:
            continue                          # seed ALL scanned pages, not just page 1
        if unknown == 0:
            break                             # caught up with newest-first results
        total = int(data.get("total", 0) or 0)
        offset += int(SEARCH_PARAMS["limit"])
        if fetched >= total or not items:
            break
    if baseline:
        meta_set(conn, "baseline_done", "1")

    overflow = 0
    if len(new_items) > MAX_ALERTS_PER_TICK:  # flood guard: newest first
        overflow = len(new_items) - MAX_ALERTS_PER_TICK
        for it in new_items[MAX_ALERTS_PER_TICK:]:
            mark_seen(conn, it)
        new_items = new_items[:MAX_ALERTS_PER_TICK]
        conn.commit()
    return new_items, fetched, overflow


def main() -> int:
    setup_logging()
    lock = open(DATA_DIR / ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("skip: previous watcher run still active")
        return 0

    conn = db_connect()
    fixture = dotenv_fallback("EBAY_WATCHER_FIXTURE")
    was_baseline = meta_get(conn, "baseline_done") != "1"
    try:
        if fixture:  # test hook: items come from a file, no network
            items = json.loads(pathlib.Path(fixture).read_text())
            fetched = len(items)
            overflow = 0
            if was_baseline:
                for it in items:
                    mark_seen(conn, it)
                meta_set(conn, "baseline_done", "1")
                new_items = []
            else:
                new_items = []
                for it in items:
                    iid = str(it.get("itemId", ""))
                    if not iid or conn.execute(
                            "SELECT 1 FROM seen WHERE item_id=?", (iid,)).fetchone():
                        continue
                    fp = fingerprint(it)
                    if str(it.get("conditionId", "")) != KEEP_CONDITION or \
                       conn.execute("SELECT 1 FROM seen_fp WHERE fp=?", (fp,)).fetchone():
                        mark_seen(conn, it)
                        continue
                    new_items.append(it)
                if len(new_items) > MAX_ALERTS_PER_TICK:
                    overflow = len(new_items) - MAX_ALERTS_PER_TICK
                    for it in new_items[MAX_ALERTS_PER_TICK:]:
                        mark_seen(conn, it)
                    new_items = new_items[:MAX_ALERTS_PER_TICK]
            conn.commit()
        else:
            new_items, fetched, overflow = fetch_new_items(conn)

        if new_items:
            send_listing_alerts(new_items, overflow)  # marks seen only after delivery
            for it in new_items:
                mark_seen(conn, it)
            conn.commit()
            log.info("alerted %d new listing(s)%s", len(new_items),
                     f" (+{overflow} capped)" if overflow else "")
            print(f"ok: alerted {len(new_items)} new listing(s)"
                  + (f", {overflow} capped" if overflow else ""))
        elif was_baseline:
            log.info("baseline established (%d listings seeded)", fetched)
            print(f"baseline established: {fetched} existing listings seeded, "
                  f"future runs alert on new only")
        else:
            log.info("checked %d results — no new listings", fetched)
            print(f"ok: {fetched} results checked, no new listings")
        meta_set(conn, "last_error_fp", "")  # clear fault latch on success
    except WatcherError as exc:
        alert_fault(conn, str(exc))
        log.error("handled fault: %s", exc)
        print(f"fault (reported with dedup): {exc}")
        return 0  # handled: script self-reports; cron stays quiet
    except Exception:
        log.exception("unhandled watcher crash")
        print("watcher crashed — see ebay-watcher/watcher.log")
        return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())