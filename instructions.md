# Running finmes + Hermes Agent

The whole stack runs from docker compose: Hermes Agent as the chat/Q&A layer
(port 8642, loopback only), hledger as the ledger backend, and the ingest
endpoint the iPhone shortcut POSTs to (port 8643, reached via Tailscale).
Same layout on the dev Mac and the always-on MacBook Air.

## Fresh machine (clone-and-run)

1. Docker Desktop running.
2. `cp .env.example .env` and fill it in. The OpenRouter key must be an
   **inference** key (openrouter.ai → Keys) — provisioning/management keys
   cannot do inference and fail with `401 User not found`.
3. Clone the private data repo into `data/`:

   ```sh
   git clone git@github-personal:unnxt30/finmes-data.git data
   ```
4. ```sh
   docker compose up -d --build
   ```

That is everything. The one-shot `hermes-env` service regenerates
`.hermes/.env` from the root `.env` on every `up`, so the root `.env` is the
single secrets file. Hermes model/provider config (`.hermes/config.yaml`) and
the finmes-hledger skill are tracked in git. The rest of `.hermes/` (sessions,
trajectory db, caches) is runtime state that regenerates.

Health checks:

```sh
curl -s http://localhost:8643/health                     # ingest
KEY=$(grep API_SERVER_KEY .env | cut -d= -f2)
curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $KEY" \
  http://localhost:8642/health                           # hermes gateway
```

## Layout

```
finmes/
├── docker-compose.yml  # the whole stack — hermes gateway + ingest endpoint
├── Dockerfile          # nousresearch/hermes-agent + hledger
├── .env                # ALL secrets (gitignored; template in .env.example)
├── .hermes/            # Hermes state — only config.yaml + skills/finmes-hledger tracked
├── data/               # = clone of private finmes-data repo (gitignored here)
│   ├── journal/        #   main.journal + per-year files — LEDGER_FILE points here
│   ├── mappings.toml   #   payee/account mapping tables (ask-once, reloaded per request)
│   └── ingest.jsonl    #   append-only raw SMS log — source of truth, replayable
├── finmes/parser.py    # SMS → structured transaction parser
├── finmes/journal.py   # Txn → hledger entries (dedup, mapping, rendering)
├── finmes/server.py    # POST /ingest — phone sends every SMS, all filtering here
├── finmes/discord_bot.py  # Discord service — commands, digests, payee review
├── finmes/cli.py       # `finmes backfill <corpus.tsv>`
└── scripts/            # extraction / corpus tooling
```

## Discord (optional)

The stack runs a dedicated `discord-bot` compose service. It reads the journal,
calls Hermes for natural-language Q&A, and posts scheduled digests. Hermes does
not start a Discord adapter.

### One-time setup

1. At discord.com/developers, create an application. On the Bot tab, enable
   **Message Content Intent** and copy the bot token.
2. On OAuth2 → URL Generator, select scopes `bot` and `applications.commands`.
   Grant Send Messages and Read Message History. Open the URL and invite the
   bot to a private server.
3. In Discord, enable Developer Mode. Right-click your user name and the digest
   channel to copy their IDs.
4. Set these variables in `.env`:
   - `DISCORD_BOT_TOKEN` — bot token from step 1
   - `DISCORD_ALLOWED_USERS` — comma-separated Discord user IDs allowed to use
     the bot (commands, buttons, and Q&A)
   - `DISCORD_HOME_CHANNEL` — channel ID where scheduled digests are posted
5. Run `docker compose up -d`. The `discord-bot` service starts when the token
   is set.

### Slash commands

| Command | Purpose |
|---|---|
| `/spend` | Category spending totals for a period |
| `/recent` | Latest expense transactions |
| `/search` | Find transactions by description |
| `/digest` | On-demand spending digest for a period |
| `/review` | Interactive cards for unmapped payees |

### Natural-language Q&A

Authorized users can ask finance questions in two ways:

- Send a direct message to the bot (full message text goes to Hermes).
- Mention the bot in a guild channel (text after the mention goes to Hermes).

### Scheduled digests

The bot posts to `DISCORD_HOME_CHANNEL`:

- **Weekly** — every Monday at 08:00 IST (`lastweek` category and payee totals)
- **Month-end** — on the 1st of each month at 08:00 IST (`lastmonth` totals)

Each digest lists category spending, top payees, and the unknown-payee count.
When unknown payees exist, the message includes a **Review Unmapped** button
that opens the same interactive review flow as `/review`.

### Payee mapping workflow

Unknown debit payees land in `expenses:unknown` until mapped. Use `/review` or
the digest button to assign a category. Each mapping writes to `mappings.toml`
and triggers a journal rebuild that replaces only the derived `YYYY.journal`
files from the raw ingest log. Historical entries update to the new account.

## Ingest endpoint

The shortcut POSTs `{text, sender, received_at, location}` with
`Authorization: Bearer $FINMES_INGEST_TOKEN` to `http://<tailnet-ip>:8643/ingest`.
Response statuses: `parsed` (in the journal; `duplicate: true` if already
there), `skipped` (bank noise, logged), `unparsed` (logged for review),
`ignored` (not a bank SMS — never persisted). Financial-looking messages are
appended to `ingest.jsonl` *before* the journal, so the journal is always
regenerable from raw.

## Talk to it

### Interactive chat in the terminal (best for playing around)

```sh
docker exec -it hermes hermes
```

Opens the Hermes CLI inside the running container, sharing the same config
and memory. Try:

> How much did I spend this month, and what is my bank balance?

(The journal location comes from `LEDGER_FILE`; the finmes-hledger skill
tells the agent to never pass `-f`.)

Useful in-chat commands: `/model` (switch models), `/new` (fresh session),
`/quit`.

### OpenAI-compatible HTTP API

```sh
KEY=$(grep API_SERVER_KEY .env | cut -d= -f2)
curl -s http://localhost:8642/v1/chat/completions \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "hermes-agent", "messages": [{"role": "user",
       "content": "Using hledger: what did I spend in June 2026, by category?"}]}'
```

Agent runs with tool calls take a while (30s+). That is normal.

## Everyday operations

| Task | Command |
|---|---|
| Logs | `docker compose logs -f hermes` / `docker compose logs -f ingest` |
| Restart (e.g. after editing root `.env`) | `docker compose up -d` (regenerates `.hermes/.env` too) |
| Stop everything | `docker compose down` |
| Rebuild after Dockerfile change | `docker compose up -d --build` |
| Run hledger directly | `docker exec hermes hledger -f /opt/journal/main.journal balance` |
| Backfill SMS corpus into the journal | `.venv/bin/finmes backfill` (idempotent; re-run any time) |
| Export agent traces (training corpus) | `docker exec hermes hermes sessions export --format jsonl - --yes > traces/$(date +%Y%m%d).jsonl` |
| Replay raw ingest log into the journal | `.venv/bin/finmes replay` (idempotent) |
| Look up a payment location | `docker exec hermes nearby <lat> <lon>` |

## Agent traces

Every Hermes session (system prompt, messages, tool calls, terminal outputs,
final answers, token/cost stats) is persisted in `.hermes/state.db` and kept
forever — `sessions.auto_prune: false` is pinned in `.hermes/config.yaml` so
an upstream default change cannot silently delete it. This is the training
corpus for a possible future fine-tuned local model, and the eval set for
trying cheaper hosted models. Context compaction only soft-archives rows
(`active=0, compacted=1`); originals stay on disk.

Export trajectories any time:

```sh
docker exec hermes hermes sessions export --format jsonl - --yes
```

(`--format trace` emits Hugging Face Agent Trace Viewer format; `--redact`
scrubs secrets. Filters like `--newer-than`, `--min-tool-calls` available.)

## Troubleshooting

- **`401 User not found` from chat** — the *OpenRouter* key is bad (wrong
  type, revoked, or mis-pasted). Verify with:
  `curl -s https://openrouter.ai/api/v1/key -H "Authorization: Bearer <key>"`
  and check the reply does NOT say `is_provisioning_key: true`.
- **Empty reply on port 8642** or **`Refusing to start: API_SERVER_KEY is
  required`** — `API_SERVER_KEY` missing from the root `.env`. Fix it there
  and `docker compose up -d` (regenerates `.hermes/.env`).
- **Tests** — `.venv/bin/pytest` (parser, journal, ingest server — all over
  real SMS fixtures); coverage report: `.venv/bin/python scripts/parse_corpus.py`.
