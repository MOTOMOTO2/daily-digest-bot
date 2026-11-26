# daily-digest-bot

Collects items from sources you configure — RSS feeds, JSON APIs, and pages it
scrapes — deduplicates them against everything it has already sent, and posts
**one digest a day** to a chat channel. Everything it knows lives in a local
SQLite file, or in DynamoDB when it runs on AWS Lambda, and when something
breaks it says so loudly instead of going quiet.

Python for the bot and its API; React for the dashboard.

```
┌─────────────┐
│  RSS feed   │──┐
├─────────────┤  │   ┌───────────┐   ┌──────────┐   ┌─────────┐
│  JSON API   │──┼──▶│ collect + │──▶│  SQLite  │──▶│ one     │──▶ #your-channel
├─────────────┤  │   │ dedupe    │   │  record  │   │ digest  │
│ scraped page│──┘   └───────────┘   └──────────┘   │ a day   │
└─────────────┘            │               │        └─────────┘
                           ▼               ▼              │
                    logs/digest-bot.log  events ──────────┴──▶ React dashboard
```

## The part that matters: it fails loudly

A digest bot's worst failure mode is silence — the channel goes quiet, and
nobody can tell whether the news was slow or the bot died three weeks ago. This
one is built so that can't happen:

| Failure | What the bot does |
|---|---|
| One source errors out | Logs `ERROR`, records it, **still posts the digest**, and lists the failure in a "Collection problems" section inside the message itself |
| A scraped page gets redesigned | The selector matching nothing is an error, not an empty result — the classic silent failure is an explicit one |
| An API changes shape | `items_path`/`fields` failing to resolve is an error that names what the response actually contained |
| Nothing new to report | Still posts, saying "no new items" — so a quiet channel always means a broken bot |
| Delivery fails | Logs `CRITICAL`, marks the digest `failed`, **releases its items back into the queue** so nothing is lost, pings a separate alert channel, and exits non-zero |
| Yesterday's digest never went out | Its items are folded into today's, with a warning |
| The whole process crashes | The run row is marked `failed` with the traceback in the log, and the exception propagates to a non-zero exit |
| No digest for `max_silence_hours` | `digest-bot healthcheck` exits `4` — point an uptime monitor at it and silence becomes an alert |

Warnings and errors go to three places at once: the rotating log file, the
`events` table in SQLite, and the dashboard.

## Try it in one command

No config, no webhook, no network. This spins up a throwaway HTTP server,
points the bot at it as all three source kinds, and walks the whole lifecycle —
a healthy day, a duplicate day, a broken page and API, a dead webhook, and the
recovery:

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows;  source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt

python scripts/demo.py
```

It starts the server, does its work, and exits — nothing is left listening.
Pass a path (`python scripts/demo.py .demo`) to keep the result somewhere you
can point the dashboard at.

## Real setup

### 1. Install

```bash
pip install -r requirements.txt     # or: pip install -e ".[server,dev]"
```

Python 3.11+ (it uses the standard library's `tomllib`).

### 2. Configure

```bash
cp config.example.toml config.toml
cp .env.example .env
```

Secrets never go in `config.toml`. Any `${VAR}` in it is read from the
environment or `.env`, and a **missing variable is a hard error** — you get a
clear message at startup rather than an empty webhook URL and a digest that
quietly goes nowhere.

```toml
[bot]
timezone = "America/Toronto"
send_after = "08:00"          # earliest local time the day's digest may go out
lookback_hours = 72           # older items are recorded but never posted
post_when_empty = true        # keep this true; it is the proof-of-life signal
max_silence_hours = 26        # healthcheck fails past this
dedupe_by = ["url", "title"]

[delivery]
kind = "slack"                # slack | discord | webhook | file | stdout
webhook_url = "${SLACK_WEBHOOK_URL}"
channel = "#daily-digest"

[alerts]                      # only used when the digest itself can't be sent
enabled = true
kind = "slack"
webhook_url = "${ALERT_WEBHOOK_URL}"
channel = "#bot-alerts"
```

Point `[alerts]` at a *different* channel from `[delivery]` — a channel that is
down cannot report that it is down.

### 3. Add sources

Three kinds, all declarative. `name` must be unique; it is the key used in the
local record and the dashboard.

**RSS or Atom:**

```toml
[[sources]]
name = "hacker-news"
kind = "rss"
url = "https://hnrss.org/frontpage"
label = "Hacker News front page"   # shown in the digest instead of `name`
max_items = 10
exclude = ["hiring"]               # case-insensitive, matched on title + summary
```

**A JSON API** — `items_path` is a dotted path to the list (`""` means the body
*is* the list), and `fields` maps digest fields to dotted paths inside each
element:

```toml
[[sources]]
name = "cpython-releases"
kind = "api"
url = "https://api.github.com/repos/python/cpython/releases"
items_path = ""
headers = { Accept = "application/vnd.github+json" }

[sources.fields]
id = "id"                  # a source-provided id is the best dedupe key
title = "name"
url = "html_url"
summary = "body"
author = "author.login"    # dotted paths walk nested objects and list indices
published_at = "published_at"
```

**A page to scrape** — `selectors.item` picks each row, the rest select inside a
row, and `@attr` reads an attribute instead of the text:

```toml
[[sources]]
name = "python-blog"
kind = "scrape"
url = "https://blog.python.org/"

[sources.selectors]
item = "div.post"
title = "h3.post-title a"
url = "h3.post-title a@href"       # relative URLs resolve against the page
summary = "div.post-body"
published_at = "abbr.published@title"
```

### 4. Prove it works

```bash
digest-bot init            # validates the config, creates the database
digest-bot test-channel    # posts a test message — proves delivery before you rely on it
digest-bot run --dry-run   # builds a real digest and prints it, sends nothing
```

### 5. Schedule it

There is no resident daemon. `digest-bot run` is a one-shot command, safe to
call as often as you like: it **collects every time** and **posts at most once
per calendar day** (enforced by a `UNIQUE` constraint on the digest date, not
just an `if`). Running it hourly means the digest goes out on the first run
after `send_after`, and a transient outage at 08:00 is retried at 09:00 rather
than losing the day.

**cron** (hourly collect-and-maybe-post, plus a healthcheck that shouts):

```cron
0 * * * *  cd /srv/digest-bot && .venv/bin/digest-bot run --trigger cron
30 9 * * * cd /srv/digest-bot && .venv/bin/digest-bot healthcheck || curl -fsS "$HEARTBEAT_FAIL_URL"
```

**AWS Lambda**, with no machine to keep on: EventBridge Scheduler runs it
hourly, DynamoDB holds the record, and a CloudWatch alarm emails on failure.
GitHub Actions deploys it. See [deploy/aws/README.md](deploy/aws/README.md).

**Windows Task Scheduler:**

```powershell
schtasks /create /tn "daily-digest-bot" /sc hourly `
  /tr "'D:\daily-digest-bot\.venv\Scripts\digest-bot.exe' --config 'D:\daily-digest-bot\config.toml' run --trigger scheduler"
```

## Commands

| Command | What it does |
|---|---|
| `run` | Collect, then post today's digest if it is due. `--force`, `--dry-run`, `--strict`, `--trigger` |
| `collect` | Poll sources and record new items; send nothing |
| `healthcheck` | Exit non-zero if the bot is unhealthy or has gone quiet |
| `status` | Human-readable summary of the local record |
| `sources` | Per-source health, error history and reliability |
| `preview` | Render what the next digest would look like |
| `digests` | List recorded digests; `--show ID` prints one |
| `log` | The bot's recent warnings and errors; `--level` filters |
| `test-channel` | Post a test message to the configured channel |
| `prune --days N` | Drop old raw payloads, events and runs — **never fingerprints** |
| `serve` | Run the read-only dashboard API |
| `init` | Validate the config and create the database |

Add `--json` to any of them for machine-readable output.

### Exit codes

The exit code is the contract with cron:

| Code | Meaning |
|---|---|
| `0` | Fine |
| `1` | Unexpected crash (traceback in the log) |
| `2` | The digest could not be delivered |
| `3` | A source failed, with `--strict` |
| `4` | `healthcheck` says unhealthy |
| `5` | Bad configuration |

A source failing exits `0` by default: one broken feed should not fail the
whole cron job, and it is reported in the digest and the dashboard regardless.
Use `--strict` if you'd rather hear about it from cron too.

## Dashboard

```bash
cd dashboard && npm install && npm run build   # once
digest-bot serve                               # http://127.0.0.1:8000
```

`serve` hosts both the API and the built dashboard. For UI development,
`npm run dev` on port 5173 proxies `/api` to a running `serve`.

Five views: **Overview** (hero "hours since the last digest", health checks,
stat tiles, per-source cards), **Digests** (items-per-day chart and full
history with the exact body that was posted), **Runs**, **Log** (events plus a
tail of the real log file), and **Queue** (what is waiting for the next digest).
Dark mode follows the OS and can be toggled; tabs are deep-linkable (`#digests`).

The dashboard is **read-only** by design — it shows whether the bot is working;
it can't fire a digest or edit the record. Collection and delivery stay in the
CLI where cron drives them.

## How deduplication works

Each item gets a fingerprint, stored under a `UNIQUE` constraint — so the
database, not application logic, is what guarantees an item is recorded once:

1. **A source-provided id** (RSS `guid`, API `fields.id`) wins when present.
2. Otherwise the **canonical URL**: lowercased host, `www.` and tracking
   parameters (`utm_*`, `gclid`, `fbclid`, …) stripped, query sorted, fragment
   dropped. `https://www.Example.com/post/?utm_source=rss#top` and
   `https://example.com/post` are the same item.
3. Otherwise the **normalized title** (case-, accent- and punctuation-folded).

With `dedupe_by = ["url", "title"]` the title key is also checked separately, so
the same story arriving from two different feeds under two different URLs is
posted once. Drop `"title"` if you want each source's copy kept.

Fingerprints are kept **forever**, including by `prune` — they are the
deduplication memory. Only bulky payloads and old history get cleared.

## The local record

SQLite, at `data/digest.db`:

| Table | Holds |
|---|---|
| `items` | Every item ever seen, its fingerprint, and which digest it went out in |
| `digests` | One row per day — `UNIQUE(digest_date)` is what enforces one a day |
| `runs` | One row per invocation, with status and error |
| `source_results` | Per-source outcome per run; consecutive failures derive from this |
| `events` | Warnings and errors mirrored out of the log |

An item with `digest_id IS NULL` is collected but unsent. An item that aged past
`lookback_hours` before any digest went out gets `expired_at` set — recorded
forever for dedupe, never posted.

### Or DynamoDB

Set `database = "dynamodb://<table>"` and the same record lives in one DynamoDB
table (`pip install -e ".[aws]"`). DynamoDB has no UNIQUE constraints, so each
guarantee above is rebuilt from its own primitives: the fingerprint and the
digest date are claimed by marker records written in a conditional
transaction, ids come from an atomic counter, and the unsent queue is a sparse
index that only pending items appear in. The mapping is in
[`digestbot/dynamo.py`](digestbot/dynamo.py) and
[deploy/aws/README.md](deploy/aws/README.md#the-dynamodb-design).

## Project layout

```
digestbot/
  cli.py           commands and exit codes
  config.py        TOML + ${ENV}, validated strictly (unknown keys are errors)
  pipeline.py      collect → dedupe → build → deliver, and the failure policy
  store.py         SQLite; the UNIQUE constraints that enforce the guarantees
  dynamo.py        the same record in DynamoDB: conditional writes for the guarantees
  aws_lambda.py    the Lambda entry point: scheduled runs and health checks
  models.py        Item, fingerprinting, URL canonicalisation
  render.py        the digest body, including the "Collection problems" section
  health.py        silence detection and per-source health
  logging_setup.py rotating file + stderr + the events table
  api.py           read-only FastAPI for the dashboard
  sources/         rss.py, api.py, scrape.py behind a small registry
  delivery/        slack.py, discord.py, webhook.py, file.py (+stdout)
deploy/aws/        CloudFormation, the Lambda build, and the deploy runbook
dashboard/         React + TypeScript + Vite
scripts/demo.py    end-to-end demo against a throwaway local server
tests/             248 tests
```

## Tests

```bash
pytest -q                            # 248 tests, no network: the shared ones run
                                     # on SQLite and on DynamoDB (simulated by moto)
cd dashboard && npm run build        # type-check + build
python scripts/demo.py               # end-to-end over real HTTP
```

HTTP is mocked with `respx`, so the suite covers the failure paths that are hard
to reproduce on demand: 500s, timeouts, DNS failures, revoked webhooks,
redesigned pages, APIs that change shape, and adapters that crash outright.

## Notes and assumptions

A few calls made while building this, since the brief left them open:

- **"A channel" is a webhook.** Slack incoming webhooks are the default;
  Discord and a generic JSON webhook work the same way. `file` and `stdout`
  channels exist so the whole pipeline can run without any webhook at all.
- **Scheduling is external.** A cron entry or a scheduled task is more robust
  than a resident process, so `run` is one-shot and idempotent rather than a
  daemon with an internal clock.
- **The dashboard is read-only**, for the reason given above.
- **Empty digests are posted** (`post_when_empty = true`). This is the single
  most important default here: it is what makes silence diagnostic.
- `prune` never deletes fingerprints, so pruning can't resurrect old items.
