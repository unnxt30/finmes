# Running finmes + Hermes Agent

The whole stack runs from docker compose: Hermes Agent as the chat/Q&A layer
(port 8642, loopback only), hledger as the ledger backend, and the ingest
endpoint the iPhone shortcut POSTs to (port 8643, reached via Tailscale).
Same layout on the dev Mac and the always-on MacBook Air.

## Fresh machine (clone-and-run)

1. Docker Desktop running.
2. `cp .env.example .env` and fill it in. The OpenRouter key must be an
   **inference** key (openrouter.ai → Keys) — provisioning/management keys
   can't do inference and fail with `401 User not found`.
3. Clone the private data repo into `data/`:

   ```sh
   git clone git@github-personal:unnxt30/finmes-data.git data
   ```
4. ```sh
   docker compose up -d --build
   ```

That's everything. The one-shot `hermes-env` service regenerates
`.hermes/.env` from the root `.env` on every `up`, so the root `.env` is the
single secrets file. Hermes model/provider config (`.hermes/config.yaml`) and
the finmes-hledger skill are tracked in git; the rest of `.hermes/` (sessions,
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
├── finmes/cli.py       # `finmes backfill <corpus.tsv>`
└── scripts/            # extraction / corpus tooling
```

## Discord (optional)

Chat with the agent from Discord; cron digests land in a channel. One-time:

1. discord.com/developers → New Application → Bot tab → enable **Message
   Content Intent** → copy the bot token.
2. OAuth2 → URL Generator → scope `bot`, permissions Send Messages + Read
   Message History → open the URL and invite the bot to a private server.
3. In Discord settings enable Developer Mode, then right-click to Copy ID:
   your user ID (`DISCORD_ALLOWED_USERS`) and the digest channel
   (`DISCORD_HOME_CHANNEL`).
4. Fill the three `DISCORD_*` vars in `.env`, then `docker compose up -d`.
   The gateway starts the Discord adapter automatically when the token is
   present; only allowed users get responses.

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

> How much did I spend this month, and what's my bank balance?

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

Agent runs with tool calls take a while (30s+); that's normal.

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
an upstream default change can't silently delete it. This is the training
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
