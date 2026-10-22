# pigeonlabs-gpu-watcher

A **personal, single-user** eBay bargain-alert tool. It polls the official
eBay Developers APIs on a fixed schedule and pushes new-listing alerts to the
owner's **private Discord channel** — that is its entire external footprint.

> **Repo labelling (read this first)**
> This repository is the *public description* of a private, non-distributed
> home tool. The code published here is **sanitised**: it contains no API
> keys, no OAuth secrets, no webhook URLs, no personal data. All credentials
> are read from environment variables at run time (see *Configuration*).
> The live state database, logs and token cache live on the owner's private
> home server and are not, and will never be, published.

## What it does

- Polls the **eBay Browse API** (`searchItems`) every 2 minutes for one fixed saved search:
  - marketplace: `EBAY_GB` (eBay UK)
  - category `27386` (Computer Components → Graphics/Video Cards)
  - condition `7000` ("For parts or not working")
  - Buy It Now only, item location: United Kingdom
  - sorted newest-first
- De-duplicates against a local **SQLite** state database:
  - by `itemId` (never alert the same listing twice), and
  - by a *title + price fingerprint* (catches sellers who delete and relist
    the identical item under a new `itemId` — common with bulk refurb flippers)
- Posts genuinely new listings as rich embeds to one **Discord webhook**
  (the owner's private channel), batched 8 per message, hard-capped at 12
  alerts per tick so no single tick can flood the channel.
- First run establishes a silent **baseline** (sees the current search
  results without alerting); from then on, only new listings generate alerts.
- Runs unattended on a home server under a scheduler (a `no_agent` cron job),
  **zero LLM / inference compute**, negligible CPU.

## What it does NOT do

- No public-facing website, app, service, or API of its own
- No purchases, bids, offers, checkouts, messaging, listing/revision, feedback
- No resale, redistribution, publication, or long-term storage of eBay data
  beyond a local de-duplication index
- No collection of any personal data about eBay users; sellers are never
  identified, tracked, or contacted
- No third-party access: the alert feed is a private Discord channel owned
  by the account holder

## Flow

```
 scheduler (every 2 min)
        │
        ▼
 gpu_watcher.py ──1── eBay OAuth (client-credentials, cached token)
        │
        ├──────2── Browse API searchItems (1 saved search, newest-first)
        │
        ├──────3── SQLite diff (itemId + title/price fingerprint)
        │
        └──────4── Discord webhook → owner's private channel
```

Call volume: 1 OAuth token per ~24 h + ≤3 search calls per tick
(≤ ~2,160 calls/day) — comfortably inside the 5,000 calls/day Browse API
allocation.

## File map

| File | Purpose |
|---|---|
| `gpu_watcher.py` | The whole watcher — one self-contained, heavily commented script |

There is deliberately no build system, framework, dependency pinfile, or test
suite published: this is a single maintenance script for one user, not a
library. The only third-party dependency is `requests`.

## Configuration

All secrets come from the environment (the scheduler injects a private
`.env` file; the script can also parse it directly for manual runs).

| Variable | Required | Meaning |
|---|---|---|
| `EBAY_APP_ID` | yes | eBay Developers *production* App ID |
| `EBAY_CERT_ID` | yes | Cert ID from the **same** production keyset |
| `DISCORD_WEBHOOK_URL` | yes | Webhook of the owner's private channel |
| `EBAY_WATCHER_Q` | no | Search query (default: `"graphics card" \| gpu`) |
| `EBAY_WATCHER_SORT` | no | Sort (default: `-listing_date`) |
| `EBAY_WATCHER_FIXTURE` | no | Test hook: JSON file of item summaries, skips network |

State lives under `$HERMES_HOME/ebay-watcher/` (SQLite DB, rotating log,
cached app token — the token cache is written `0600`).

## eBay compliance notes (as implemented)

- All eBay data access is via **official eBay Developers Program APIs** with
  OAuth application tokens. No scraping, no headless browsers, no site
  automation.
- The tool stores no PII about eBay users and performs no commerce.
- Alerts link straight to the eBay listing (`itemWebUrl`) so the owner views
  items on eBay; listing data is surfaced personal-view only, consistent with
  the API License Agreement.
- Request cadence is fixed and bounded (see *Flow*); the scheduler is the
  only caller.
- A Growth Check has been requested with eBay for **Shopping API
  (Approval Level 2)** access: the Browse API index structurally misses many
  small-seller and product-catalogue listings in this category (documented
  in `gpu_watcher.py`, § *"Why the search looks like this"*), and the owner
  wants alerts to match what the eBay website itself shows — via official
  APIs, not scraping.

## Why the code is annotated the way it is

This project is maintained as self-directed learning of the eBay platform.
The comments in `gpu_watcher.py` document *decisions and observed API
behaviour* (e.g. the Browse API silently ignoring condition filters, the
flood-guard incident and its fix, token-cache layout) so a reader coming in
cold understands not just *what* runs, but *why* — which is also the point of
this public repo: an honest, readable description of a small personal tool.

---

Single owner, single user, no commercial use, not distributed.
