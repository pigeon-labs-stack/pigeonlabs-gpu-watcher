# PigeonLabs GPU Watcher

A personal, single-user eBay bargain-alert tool. It runs on a private home server and notifies the owner — and only the owner — when new relevant listings appear.

## What it does

- Polls the **eBay Browse API** (`searchItems`) on a fixed schedule for one saved search:
  - Category 27386 (Computer Components → Graphics Cards)
  - Condition 7000 ("For parts or not working")
  - Buy It Now, item location: United Kingdom
  - Sorted by newest first
- De-duplicates results locally (SQLite state database; also fingerprints title + price to catch bulk relisters)
- Pushes new listings as rich alert cards to the **owner's private Discord channel** via a Discord webhook
- Establishes a silent baseline on first run; only genuinely new listings ever generate an alert

## What it does not do

- No public-facing website, app, or service — the only output is the owner's private Discord feed
- No purchases, bids, offers, checkouts, messages, or listing creation
- No resale, redistribution, storage, or publication of eBay data
- No user data collection of any kind; eBay users are never identified or tracked
- Not distributed to third parties

## Compliance

- All eBay API calls use OAuth application tokens via the official eBay Developers Program
- Call volume is bounded by the poll schedule (well under the default 5,000 calls/day)
- Attribution: listing links go directly to eBay; the tool surfaces eBay's own listing data (title, price, condition, seller, link) in personal alerts only, consistent with the API License Agreement display requirements for personal use

Built and maintained by the owner as a self-directed project learning the eBay platform. Single user, no commercial use.
