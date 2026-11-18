# finmes

Personal finance pipeline: bank SMS received on an iPhone become an
[hledger](https://hledger.org) journal on a VPS. Ask questions in Discord through
[Hermes Agent](https://github.com/NousResearch/hermes-agent), and review unknown
payees using Mappls context and OpenAI Decisions suggestions.

**Current deployment:** GCP VPS, Docker Compose, HTTPS ingestion at
`https://finmes.unnat.xyz/ingest`. The iPhone retains its existing bank/keyword
filter. The Mac is needed only for catching up from synced Messages, not for
normal ingestion. Tailscale is not required for the VPS deployment.

Amounts and transaction details come from deterministic parsing. Hermes reads
the journal; category suggestions only become mappings after you confirm them.

## Features

| Feature | What it does |
| --- | --- |
| iPhone SMS ingestion | Accepts authenticated JSON over HTTPS, with receipt time and optional location. |
| Deterministic transaction parsing | Extracts supported bank SMS templates for UPI, cards, NEFT, IMPS, autopay and other transfers. Unknown formats are retained for inspection, not guessed. |
| Double-entry journal | Stores hledger postings, masked source accounts, payees, references and optional receipt-time/GPS tags. |
| Safe retries | Transaction keys prevent duplicate journal entries when a message is resent, replayed or recapped. Raw log records may still repeat. |
| Account and payee mappings | Resolves source accounts from masked IDs/sender prefixes/content markers and categories from exact payee mappings. |
| Commuting rule | Categorizes otherwise unmapped INR debits under ₹100 in the configured commute windows. |
| Discord finance questions | Sends authorized users' questions to Hermes, which queries hledger over a read-only journal mount. |
| Category reviews | `/review` presents an unknown payee, examples and an optional AI suggestion; explicit confirmation saves it. |
| Mappls context | Uses recorded payment-location tags to fetch nearby places, cached for 24 hours. Nearby places are unverified possibilities. |
| OpenAI Decisions | Suggests from built-in and saved custom categories, or returns unknown. It complements Hermes through a separate direct OpenAI key. |
| Historical updates | Confirmed mappings update matching unknown journal history and apply to future transactions; backups are made before rewriting. |
| Offline catch-up | Recovers an inclusive date range from the Mac's synced Messages database and sends it to the VPS. |
| CLI maintenance | Supports corpus backfill, raw-log replay, TSV category review and preview/apply recategorization. |
| Persistent state | Finance files, pending reviews and Hermes history survive container restarts through mounted storage. |

## How it works

```mermaid
flowchart TD
    A[iPhone bank SMS] --> B[Existing Shortcut filter]
    B -->|HTTPS + bearer token| C[VPS ingest endpoint]
    C --> D[Deterministic parser + raw log]
    D -->|Supported transaction, new key| E[hledger journal]
    M[Confirmed mappings + commuting rule] --> E
    D -->|Unmatched financial message| U[Raw log for parser review]
    E --> H[Hermes: read-only finance queries]
    H <--> Q[Discord chat]
    E -->|Unknown category| R[Discord review queue]
    R --> P[Mappls when GPS is available]
    P --> O[Decisions category suggestion]
    O --> V[Review card: choose / refresh / dismiss]
    V -->|User confirms| M
```

- Financial-looking messages are logged before journal writes. Non-financial
  messages return `ignored` without being persisted.
- `unparsed` messages have no journal transaction; they need a parser/template
  fix. `/review` handles **parsed transactions with unknown payee categories**.
- The raw log, source corpus and confirmed mappings support replay. Preserve
  the journal too: historical entries may not exist in the current raw log.
- Decisions currently classifies payee categories only. Sending all SMS for
  Decisions-based filtering is an experiment under consideration, **not enabled**.

## The iOS side

Keep the existing Messages automation and the Shortcut's bank/keyword matching
for now. No phone-side change is required to use Discord category reviews.

The Shortcut POSTs the message body, receipt timestamp and optional phone location
to `https://finmes.unnat.xyz/ingest`, using
`Authorization: Bearer <FINMES_INGEST_TOKEN>` and JSON content type:

   ```json
   {
     "text": "<message body>",
     "sender": "ios",
     "received_at": "2026-07-22T14:03:21+05:30",
     "location": {"lat": "28.65…", "lon": "77.44…"}
   }
   ```

   The response `summary` ("₹165.00 debit, ZEPTOMARKETPLAC") is shown as a
   notification, so every spend gets a glanceable confirmation.

| status | meaning | persisted? |
|---|---|---|
| `parsed` | matched a template, in the journal (`duplicate: true` if already there) | raw log + journal |
| `skipped` | bank noise — OTPs, declines, pre-debit notices | raw log |
| `unparsed` | bank-shaped but no template matched — raw log for parser review | raw log |
| `ignored` | not a bank SMS | nothing |

Hard-won gotchas baked into this design:

- **The current Shortcut uses `sender: "ios"`.** Account resolution therefore
  relies on message content: `[accounts]` masked IDs and `[markers]` raw-text
  fallbacks in `mappings.toml`. The Shortcut matching step remains unchanged.
- **`lat`/`lon` arrive as strings**, not numbers. Location provides optional Mappls
  context during category review; it does not establish the merchant identity.
- **The VPS URL works independently of the Mac.** For the optional local
  deployment, Tailscale Serve can expose port 8643 to your tailnet instead.
- **macOS firewall silently drops inbound** for CLI python — the interpreter
  binary needs its own allowlist entry, separate from Python.app.
- Debugging shortcuts blind is miserable; iCloud syncs them to
  `~/Library/Shortcuts/Shortcuts.sqlite` on the Mac — the `ZSHORTCUTACTIONS.ZDATA`
  blob → `plutil` → JSON gives a full action-level audit of what a shortcut
  actually does.

## Catch up missed messages

Wait until the missed SMS are visible in the Mac's Messages app after iCloud
sync. Run this **on the Mac, outside Docker**, from the project folder:

```sh
python3 -m finmes.cli recap 2026-10-13 2026-10-18 --url https://finmes.unnat.xyz/ingest
```

- Dates are inclusive **Messages receipt dates** in the Mac's local timezone.
- Give the terminal Full Disk Access in macOS Settings so it can read
  `~/Library/Messages/chat.db`. The database is read without being modified.
- The command uses `FINMES_INGEST_TOKEN` from the environment or the local `.env`
  and sends messages and receipt timestamps to the VPS for logging and parsing.
- The Mac's journal stays unchanged. Overlapping ranges and retries are safe
  because existing transaction keys are skipped. The result reports added,
  duplicate and unparsed/skipped counts.
- Recovered messages have no original iPhone Shortcut GPS tags.
- For an intentional local deployment only, omit `--url` to write directly to
  the Mac's `data/journal/`; that mode does not append to `ingest.jsonl`.

Normal ingestion comes directly from the iPhone Shortcut and needs no Mac.

## Try Discord and Decisions

1. Open the server containing the **finmes** bot. For finance chat, mention the
   bot, for example `@finmes What is my latest transaction?`. In a direct message,
   ask the question without a mention. Only IDs in `DISCORD_ALLOWED_USERS` work.
2. Type `/review count:10` and select the **finmes** command. The default is a
   private summary of up to 10 pending payees, ordered by uncategorized INR debit
   total. Counts of 1–10 are supported in summary mode. Fetching suggestions can
   take a little time; nothing is saved by opening the summary.
3. Scan each payee's transaction count, totals (kept separate by currency),
   proposed category, confidence and inclusion status. Suggestions below 85%
   confidence, unknown answers and provider failures start **excluded**. This
   threshold is a review aid, not a guarantee of accuracy.
4. Use **Edit or exclude a payee** to inspect examples and Mappls context, change
   the category, enter an **Other category**, or refresh the suggestion.
   **Include and return** explicitly includes that proposal even if confidence is
   low; **Exclude and return** leaves it out. **Back to summary** returns without
   changing its inclusion status. Category changes are proposals only.
5. Check the button's count, then click **Approve N payee mappings**. Only the
   included mappings shown in that summary are saved. Matching unknown history
   is updated with backups, and future transactions use the mappings. Unknown
   and excluded payees remain pending. **Close without saving** saves no mappings.

Use `/review individual:true count:10` for the original one-at-a-time flow
(1–50 payees, or omit the count to walk the current queue). Confirming or
dismissing advances to the next payee. An exact `payee` argument selects that
payee in individual mode and can reopen a dismissed review.

Confirmed custom categories become both dropdown presets **and Decisions
choices**, including accounts such as `expenses:investment`. Existing cached
suggestions can be refreshed to reconsider the updated choices. If choices fill
the dropdown, **More categories** and **Previous categories** browse every preset.

A payee with no location is still reviewable, but cannot exercise Mappls. Mac
recap messages do not contain the original phone GPS. Explicitly mapped payees
and entries already covered by the commuting rule are not in this queue.

A batch approval validates the saved proposal versions before writing. If another
review changed a selected payee, reopen the summary rather than approving a stale
card. Each payee save is independent; if a save fails halfway through a batch,
the result reports successes and failures, and pending payees can be retried.

If `/review` is absent, confirm you installed the bot with the `bot` and
`applications.commands` scopes, reopen Discord, and check the service log for
`/review registered`. If normal chat does not work, check the user allowlist and
Message Content intent in the Discord Developer Portal. A normal chat answer
checks Hermes/OpenRouter; a review suggestion checks the separate Decisions path.

On the VPS, inspect the service status and recent review errors:

```sh
docker compose -f docker-compose.yml -f docker-compose.vps.yml --profile vps ps
docker compose logs --tail=50 discord-bot
```

Do not paste `.env`, authorization headers or raw financial logs into public
support channels. A provider error or missing key never blocks ingestion.

## Categorizing unknown payees

Discord supports a Mappls + OpenAI Decisions review flow. Add a direct
`OPENAI_API_KEY` to `.env` alongside the existing Mappls and Discord keys.
The Discord frontend sends normal questions to Hermes and handles `/review`
and category confirmation buttons itself; Hermes's journal mount stays read-only.

- Use `/review` to work through pending unknown payees, newest first, or
  `/review count:10` to review up to 10. Confirming ordismissing advances to the
  next card. Optionally supply an exact payee name to reopen a dismissed review.
- GPS-tagged examples get nearby Mappls candidates (cached for 24 hours).
  Without GPS, the suggestion uses the payee and transaction context.
- Decisions selects from built-in and confirmed custom category accounts, or says unknown.
  It receives the payee name, amounts and debit or credit directions for up to three
  payments, and available nearby place descriptions. Transaction dates and times stay local to Finmes.
- **Approve N payee mappings** (summary mode) and **Remember this payee**
  (individual mode) save payee mappings. Changing a category, including it in a
  proposal, or requesting a new suggestion does not save a financial mapping.
- Use **Confirm this payment** on an individual review or in a summary's payee
  editor. Pick a dated payment and save it to apply the category to that one
  journal entry. Then Finmes checks up to 30 uncategorized payments across
  payees, prioritizing nearby records and similar amounts in the same currency
  and direction. Decisions compares them with confirmed examples using payee
  names, exact amounts, payment directions, and a coarse nearby indicator.
  The Discord card shows up to 10 matches at an estimated 85% probability or
  higher. Select the entries you want, then click **Approve N payments** to
  categorize them. Run `/review` for a fresh summary after a payment save.
- Use `/matches` to generate a review card from categories you already
  confirmed. Use `/matches category:expenses:transport:commuting` to focus on
  one category. The scan checks up to 30 candidates per category, currency and
  direction, and shows up to 10 unambiguous suggestions.
- Confirmation backs up the mapping and journal, updates matching unknown
  history, and preserves historical entries absent from the raw log. Existing
  explicit mappings are never overwritten by a stale card.
- Pending reviews survive bot restarts. Reviews are **on demand by default**:
  run `/review` to start a session. New transactions do not send unsolicited
  cards or trigger background Decisions requests. To explicitly enable automatic
  cards, set `DISCORD_REVIEW_NOTIFICATIONS=true` and recreate the Discord service.
- API failures and uncertain answers leave manual category selection available.
  They never block SMS ingestion or auto-save a category.

Review state is private runtime data at `data/review-state.sqlite3`; keep it out
of Git and include it in secure VPS backups.

The CLI batch review remains available. Run it where the authoritative data
lives (the VPS in the current setup), or prefix commands with
`docker compose exec ingest` to use the deployed container:

Export the unknown payees, ordered by total INR debit amount:

```sh
python3 -m finmes.cli review
```

Open `data/payee-review.tsv`. The `suggestion` column is a hint for a few
obvious merchant names and is never applied automatically. Fill only the
`category` column for payees you recognize (for example,
`expenses:groceries` or `assets:transfers`). Leave uncertain rows empty.
Then apply your choices to future and existing entries:

```sh
python3 -m finmes.cli apply-review
```

For an unknown INR debit **under ₹100**, the receipt time categorizes it as
`expenses:transport:commuting` during 09:00–11:59 or 15:00–19:59, on any day.
An explicit payee mapping always wins. New phone and recap entries store a
`time:` tag. To update older entries, preview then apply recovered times
from the raw ingest log and, if Terminal has Full Disk Access, Mac Messages:

```sh
python3 -m finmes.cli recategorize --db ~/Library/Messages/chat.db
docker compose stop ingest
python3 -m finmes.cli recategorize --db ~/Library/Messages/chat.db --apply
docker compose start ingest
```

The `--db` option above is for a Mac with Messages access. On the VPS, omit it
and recover available receipt times from the raw ingest log instead.

Historical entries whose receipt time is unavailable remain uncategorized
unless you map their payee. Historical rewrites save the previous journal
files under `data/backups/`.

## Running it

```sh
cp .env.example .env   # fill the required keys/tokens listed below
git clone git@github.com:unnxt30/finmes-data.git data
docker compose up -d --build
```

For a public HTTPS VPS deployment, see [VPS.md](VPS.md). The local setup builds the
hermes+hledger image, generates `.hermes/.env` from the root `.env`, and
starts the gateway (8642, loopback), ingest server (8643), and Discord frontend. Details, health
checks, Discord linking, and day-2 ops: [instructions.md](instructions.md).

## Configuration

Use `.env.example` as the template. The root `.env` configures Compose and
generates Hermes's own environment; do not edit `.hermes/.env` directly.

| Variable | Purpose |
| --- | --- |
| `FINMES_INGEST_TOKEN` | Shared secret for the phone/remote recap and ingest endpoint. |
| `API_SERVER_KEY` | Internal authenticated access from the Discord frontend to Hermes. |
| `OPENROUTER_API_KEY` | Hermes finance conversation and tool use. |
| `DISCORD_BOT_TOKEN` | Discord bot login. |
| `DISCORD_ALLOWED_USERS` | Comma-separated Discord user IDs allowed to chat and confirm. |
| `DISCORD_REVIEW_NOTIFICATIONS` | Defaults to `false`. Set `true` only to opt into automatic review cards. |
| `DISCORD_HOME_CHANNEL` | Destination for opt-in automatic cards; otherwise DM the first allowed user by numeric ID. |
| `OPENAI_API_KEY` | Direct OpenAI key for Decisions category suggestions; independent of OpenRouter. |
| `MAPPLS_API_KEY` | Optional nearby-place lookup for GPS-tagged reviews and the agent's `nearby` helper. |
| `FIRECRAWL_API_KEY` | Optional agent web search/scraping. |

Changing the laptop's `.env` does not update the VPS automatically. Edit the VPS
file and recreate the affected service. For Decisions/Mappls review settings:

```sh
docker compose -f docker-compose.yml -f docker-compose.vps.yml --profile vps up -d --no-build --no-deps discord-bot
```

## Layout

```
docker-compose.yml   Hermes, ingest, Discord frontend, optional Caddy
Caddyfile            VPS HTTPS reverse proxy
finmes/parser.py     supported SMS templates → structured transactions
finmes/journal.py    source/category mappings, deduplication, journal rendering
finmes/server.py     authenticated ingestion, raw logging and status responses
finmes/cli.py        recap, backfill, replay, review, apply-review, recategorize
finmes/categories.py category review exports and backed-up historical rewrites
finmes/enrichment.py Mappls lookup and direct OpenAI Decisions client
finmes/reviews.py    persistent review queue, authorization and confirmation
finmes/bulk_reviews.py batch snapshots, eligibility and guarded approval
finmes/bulk_discord.py batch summaries and proposal editing
finmes/discord_bot.py Discord review cards and chat bridge to Hermes
data/                private journal, mappings, raw log and review state
.hermes/             agent configuration, skill and private runtime history
scripts/             Mac Messages corpus extraction and other tooling
tests/               parsing, ingestion, journals, provider contracts and reviews
```

## Replay and backfill

Run these where the authoritative `data/` lives (the VPS for the current setup):

```sh
python3 -m finmes.cli backfill path/to/corpus.tsv
python3 -m finmes.cli replay data/ingest.jsonl
```

Both skip existing transaction keys. Replays do not fix already-written category
postings; use category review/recategorization for those. To run a CLI command
inside the deployed ingest container, prefix it with
`docker compose exec ingest`, for example:

```sh
docker compose exec ingest python -m finmes.cli review
```

## Storage, privacy and backups

- Keep `data/` private: journal, mappings, raw bank-shaped SMS and backups contain
  financial information. The application repository does not contain these files.
- Back up the journal, `mappings.toml`, `ingest.jsonl`, review SQLite database,
  Hermes runtime databases and Caddy certificate storage securely off the VPS.
  SQLite files should be backed up consistently, using SQLite backup facilities
  or while their writers are stopped.
- Review state is runtime data and is excluded from Git. `.env` and Hermes
  runtime databases must also stay out of Git. Git pushes do not back up secrets
  or all runtime state.
- Mappls receives coordinates for nearby-place searches. Decisions receives the
  limited evidence described above; Hermes receives your questions and tool
  results through OpenRouter. The financial-looking logging gate can retain OTPs
  or other bank notices even though they do not become journal transactions.
- An unavailable VPS does not provide a durable queue for failed Shortcut calls.
  Use Mac Messages recap after synchronization to recover supported messages.

## The agent

Hermes runs alongside hledger with a
[finance skill](.hermes/skills/finmes-hledger/SKILL.md) containing query recipes
and instructions to ground answers in journal results. Its journal mount is
read-only. It uses `openai/gpt-6-luna` through OpenRouter, with reasoning effort
`none` in the current configuration. Chat sessions are scoped per Discord
channel and user. Agent history is retained in `.hermes/state.db` with automatic
pruning disabled.

The Discord frontend handles review commands and confirmation callbacks, and
forwards ordinary questions to Hermes. Decisions is a separate category
classifier; it does not replace the conversational agent or parse transaction
amounts. The agent's `nearby` helper can also look up recorded location tags.

## Development

Python 3.11+ is required. Run the test suite with pytest and the project dependency:

```sh
uv run --no-project --with pytest --with 'discord.py>=2.5,<3' python -m pytest -q
```

## Planned, not enabled

Decisions-based SMS filtering, scheduled weekly/month-end digests, threshold
alerts, subscription/autopay audits, budget reports and bank-statement
reconciliation remain future work. The current Shortcut filter stays in place.
