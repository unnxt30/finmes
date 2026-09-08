# Discord Frontend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Discord bot that supports deterministic finance commands, interactive payee mapping, scheduled digests, and Hermes-backed natural-language questions.

**Architecture:** A `discord-bot` Compose service connects to Discord and mounts the private data directory. It runs deterministic hledger queries locally and sends natural-language messages to the single existing Hermes gateway. A mapping action updates the mapping table and atomically rebuilds the derived year journal files from the raw ingest log.

**Tech Stack:** Python 3.11+, `discord.py`, standard-library `urllib`, `tomllib`, `asyncio`, Docker Compose, hledger 1.52.1.

## Global Constraints

- Keep `data/ingest.jsonl` as the source of truth. Do not write journal entries directly from Discord.
- Keep the journal read-only for Hermes. Only the Discord mapping flow writes mappings and rebuilds derived year files.
- Use only `DISCORD_ALLOWED_USERS` for user authorization. Ignore unauthorized messages and interactions.
- Route natural-language queries to `http://hermes:8642/v1/chat/completions`, which is the existing Hermes service and state database.
- Do not route Discord bot traffic through the Hermes Discord adapter. The dedicated bot replaces that adapter.
- `/review` handles parsed unknown payees only. Unparsed messages require parser-template work and are out of scope.
- Keep all bot I/O non-blocking from the Discord event loop.

---

## File Structure

- `finmes/journal.py`: Add focused helpers that update a mapping table and rebuild derived year journals safely.
- `finmes/discord_queries.py`: Run fixed hledger commands and format their plain-text output for Discord.
- `finmes/discord_bot.py`: Configure the bot, authorize requests, register commands and views, call Hermes, and schedule digests.
- `tests/test_journal.py`: Cover mapping updates and atomic rebuild behavior.
- `tests/test_discord_queries.py`: Cover command construction and output chunking without hledger.
- `tests/test_discord_bot.py`: Cover authorization, Hermes requests, and component callbacks with fakes.
- `pyproject.toml`: Declare the bot runtime dependency.
- `Dockerfile`: Install the declared package and bot runtime dependency.
- `docker-compose.yml`: Add the bot service and stop injecting Discord configuration into Hermes.
- `.env.example`: State that Discord variables configure the dedicated bot.
- `README.md`, `instructions.md`: Replace Hermes Discord adapter setup with dedicated bot setup and commands.

### Task 1: Safe Mapping Updates and Journal Rebuild

**Files:**
- Modify: `finmes/journal.py`
- Modify: `tests/test_journal.py`

**Interfaces:**
- Produces `upsert_mapping(path: Path, table: Literal["payees", "markers"], key: str, account: str) -> None`.
- Produces `rebuild_journal(log_path: Path, journal_dir: Path, mappings: Mappings) -> Summary`.
- `rebuild_journal` uses the raw-log record format consumed by `finmes.cli._read_ingest_log`.

- [ ] **Step 1: Write failing tests for mapping update and rebuild**

```python
def test_upsert_mapping_adds_quoted_payee_to_payees_table(tmp_path):
    path = tmp_path / "mappings.toml"
    path.write_text('[payees]\nBlinkit = "expenses:groceries"\n')
    upsert_mapping(path, "payees", "ZEPTOMARKETPLAC", "expenses:food:groceries")
    assert Mappings.load(path).payees["ZEPTOMARKETPLAC"] == "expenses:food:groceries"


def test_rebuild_replaces_old_derived_year_entry(tmp_path):
    journal = tmp_path / "journal"
    journal.mkdir()
    (journal / "2026.journal").write_text("obsolete derived data\n")
    log = tmp_path / "ingest.jsonl"
    _write_log(log, [{"at": "2026-07-22T16:35:00", "payload": {"text": RAW, "sender": "ios"}}])
    mappings = Mappings.loads(MAPPINGS + '\n[payees]\nSANTUSHTI = "expenses:food"\n')
    rebuild_journal(log, journal, mappings)
    entry = (journal / "2026.journal").read_text()
    assert "obsolete derived data" not in entry
    assert "expenses:food" in entry
```

- [ ] **Step 2: Run the focused tests to verify failure**

Run: `.venv/bin/pytest tests/test_journal.py -k 'upsert_mapping or rebuild' -v`

Expected: FAIL because `upsert_mapping` and `rebuild_journal` do not exist.

- [ ] **Step 3: Add a TOML-table upsert helper**

Implement a narrow textual update that preserves all unrelated mapping tables:

```python
def upsert_mapping(path: Path, table: str, key: str, account: str) -> None:
    if table not in {"payees", "markers"}:
        raise ValueError(f"unsupported mapping table: {table}")
    # Locate [table], replace its exact quoted key if present, or append it.
    # Encode key and account with json.dumps for valid TOML basic strings.
```

Reject empty keys and accounts. Keep the current `[accounts]`, `[senders]`, `[markers]`, and `[payees]` table semantics unchanged.

- [ ] **Step 4: Add an atomic rebuild helper**

Build all entries in a temporary sibling directory, then replace only `YYYY.journal` derived files after `ingest()` succeeds:

```python
def rebuild_journal(log_path: Path, journal_dir: Path, mappings: Mappings) -> Summary:
    with tempfile.TemporaryDirectory(dir=journal_dir.parent) as temp:
        rebuilt = Path(temp)
        summary = ingest(_read_ingest_log(log_path), rebuilt, mappings)
        # Replace old YYYY.journal files, then move rebuilt YYYY.journal files.
    return summary
```

Move `_read_ingest_log` from `finmes.cli` into `finmes.journal` as `read_ingest_log`. Make the CLI import it. Leave `main.journal` untouched because it is the root include file.

- [ ] **Step 5: Run the journal test suite**

Run: `.venv/bin/pytest tests/test_journal.py tests/test_cli.py -v`

Expected: PASS.

- [ ] **Step 6: Commit the rebuild foundation**

```bash
git add finmes/journal.py finmes/cli.py tests/test_journal.py tests/test_cli.py
git commit -m "feat: rebuild journal after mapping changes"
```

### Task 2: Deterministic hledger Query and Formatting Module

**Files:**
- Create: `finmes/discord_queries.py`
- Create: `tests/test_discord_queries.py`

**Interfaces:**
- Produces `run_hledger(args: list[str], *, ledger_file: Path) -> str`.
- Produces `spending_report(period: str, category: str | None, *, ledger_file: Path) -> str`.
- Produces `recent_transactions(limit: int, *, ledger_file: Path) -> str`.
- Produces `search_transactions(query: str, *, ledger_file: Path) -> str`.
- Produces `chunk_text(text: str, limit: int = 1900) -> list[str]`.

- [ ] **Step 1: Write failing unit tests for fixed arguments and chunking**

```python
def test_spending_report_uses_expenses_and_period(monkeypatch, tmp_path):
    captured = []
    monkeypatch.setattr(queries, "run_hledger", lambda args, **_: captured.append(args) or "report")
    assert queries.spending_report("thismonth", None, ledger_file=tmp_path / "main.journal") == "report"
    assert captured == [["bal", "expenses", "-p", "thismonth", "-2", "-S"]]


def test_chunk_text_preserves_all_lines_under_discord_limit():
    chunks = chunk_text("a" * 1000 + "\n" + "b" * 1000, limit=1900)
    assert chunks == ["a" * 1000, "b" * 1000]
```

- [ ] **Step 2: Run tests to verify failure**

Run: `.venv/bin/pytest tests/test_discord_queries.py -v`

Expected: FAIL because the module does not exist.

- [ ] **Step 3: Implement a fixed-command query module**

Use `subprocess.run(["hledger", *args], env={**os.environ, "LEDGER_FILE": str(ledger_file)}, capture_output=True, text=True, timeout=15, check=False)`.

Raise `HledgerError` with sanitized stderr on a nonzero exit. Do not pass shell strings. Permit only periods matching `^(this|last)[a-z0-9 ]+$|^20\d\d(-\d\d)?$` and categories matching `^[a-z:]+$`.

Use these commands:

```python
["bal", "expenses", "-p", period, "-2", "-S"]
["reg", "expenses", "-n", str(limit)]
["print", f"desc:{re.escape(query)}"]
["bal", "expenses", "-p", period, "-2", "-S"]
```

- [ ] **Step 4: Run the query tests**

Run: `.venv/bin/pytest tests/test_discord_queries.py -v`

Expected: PASS.

- [ ] **Step 5: Commit deterministic queries**

```bash
git add finmes/discord_queries.py tests/test_discord_queries.py
git commit -m "feat: add deterministic Discord finance queries"
```

### Task 3: Bot Service and Hermes Client

**Files:**
- Create: `finmes/discord_bot.py`
- Create: `tests/test_discord_bot.py`
- Modify: `pyproject.toml`
- Modify: `Dockerfile`
- Modify: `docker-compose.yml`
- Modify: `.env.example`

**Interfaces:**
- Produces `parse_allowed_users(value: str) -> set[int]`.
- Produces `HermesClient(base_url: str, api_key: str, timeout: float = 45)`.
- Produces `create_bot(config: BotConfig) -> discord.Client`.

- [ ] **Step 1: Write failing tests for authorization and Hermes HTTP calls**

```python
def test_parse_allowed_users_accepts_comma_separated_ids():
    assert parse_allowed_users("10, 20") == {10, 20}


def test_hermes_client_sends_bearer_key(monkeypatch):
    request = {}
    monkeypatch.setattr(bot, "urlopen", lambda req, timeout: _Response(request, req))
    HermesClient("http://hermes:8642", "secret").answer("What did I spend?")
    assert request["value"].get_header("Authorization") == "Bearer secret"
```

- [ ] **Step 2: Run tests to verify failure**

Run: `.venv/bin/pytest tests/test_discord_bot.py -v`

Expected: FAIL because `finmes.discord_bot` does not exist.

- [ ] **Step 3: Add bot configuration and authorization**

Define an immutable `BotConfig` from environment values. Reject startup if `DISCORD_BOT_TOKEN`, `DISCORD_ALLOWED_USERS`, `DISCORD_HOME_CHANNEL`, or `API_SERVER_KEY` is absent.

Use a `commands.Bot` with `message_content=True`. Add one centralized user check for application commands, component callbacks, direct messages, and mention handling.

- [ ] **Step 4: Add the Hermes client**

Post this payload through `urllib.request` in `asyncio.to_thread`:

```python
{
    "model": "hermes-agent",
    "messages": [{"role": "user", "content": question}],
}
```

Read `choices[0].message.content`. Convert HTTP errors, invalid response JSON, and a 45-second timeout into a user-visible error string.

- [ ] **Step 5: Add Docker configuration**

Add `discord.py>=2.5,<3` to `pyproject.toml` dependencies. In the `Dockerfile`, install the project and dependencies after copying `pyproject.toml` and `finmes/`.

Add a `discord-bot` service that uses the existing hledger-enabled image, sets `working_dir: /app`, runs `python -m finmes.discord_bot`, mounts the repository at `/app`, and mounts `./data/journal` at `/opt/journal`.

Remove the `DISCORD_*` block from `hermes-env`. Hermes must not run a second Discord adapter with the same token.

- [ ] **Step 6: Run bot and existing tests**

Run: `.venv/bin/pytest -v`

Expected: PASS.

- [ ] **Step 7: Commit bot service foundation**

```bash
git add finmes/discord_bot.py tests/test_discord_bot.py pyproject.toml Dockerfile docker-compose.yml .env.example
git commit -m "feat: add Discord bot service"
```

### Task 4: Slash Commands and Natural Language Responses

**Files:**
- Modify: `finmes/discord_bot.py`
- Modify: `tests/test_discord_bot.py`

**Interfaces:**
- Consumes `spending_report`, `recent_transactions`, `search_transactions`, `chunk_text`, and `HermesClient.answer`.
- Produces `/spend`, `/recent`, `/search`, `/digest` application commands.

- [ ] **Step 1: Write failing command callback tests**

```python
async def test_spend_command_defers_then_sends_report(fake_interaction, monkeypatch):
    monkeypatch.setattr(bot, "spending_report", lambda *args, **kwargs: "₹1,000")
    await spend_command(fake_interaction, period="thismonth", category=None)
    assert fake_interaction.response.deferred is True
    assert fake_interaction.followup.messages == ["₹1,000"]
```

- [ ] **Step 2: Run tests to verify failure**

Run: `.venv/bin/pytest tests/test_discord_bot.py -k 'spend or mention or direct_message' -v`

Expected: FAIL because the handlers are missing.

- [ ] **Step 3: Implement command handlers**

For each command, defer the interaction before any subprocess call. Send query output in code blocks using `chunk_text`.

Register:
- `/spend period="thismonth" category=None`
- `/recent limit=10`
- `/search query`
- `/digest period="thisweek"`

Return hledger errors as ephemeral command replies. Limit `/recent` to 1 through 25.

- [ ] **Step 4: Implement direct message and mention routing**

For an authorized direct message, forward the complete content to Hermes. For a guild message, only forward content after the bot mention. Ignore bot-authored messages, empty questions, and unauthorized users.

- [ ] **Step 5: Run focused bot tests**

Run: `.venv/bin/pytest tests/test_discord_bot.py -v`

Expected: PASS.

- [ ] **Step 6: Commit command and Q&A support**

```bash
git add finmes/discord_bot.py tests/test_discord_bot.py
git commit -m "feat: add Discord commands and Hermes Q&A"
```

### Task 5: Interactive Unknown-Payee Review

**Files:**
- Modify: `finmes/discord_bot.py`
- Modify: `tests/test_discord_bot.py`

**Interfaces:**
- Consumes `read_ingest_log`, `Mappings.load`, `upsert_mapping`, and `rebuild_journal`.
- Produces `/review` and persistent `PayeeReviewView`.

- [ ] **Step 1: Write failing review and mapping tests**

```python
async def test_mapping_callback_updates_payees_and_rebuilds(fake_interaction, tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(bot, "rebuild_journal", lambda *args: called.append(args))
    view = PayeeReviewView(payee="ZEPTOMARKETPLAC", config=_config(tmp_path))
    await view.map_to(fake_interaction, "expenses:food:groceries")
    assert Mappings.load(tmp_path / "mappings.toml").payees["ZEPTOMARKETPLAC"] == "expenses:food:groceries"
    assert len(called) == 1
```

- [ ] **Step 2: Run tests to verify failure**

Run: `.venv/bin/pytest tests/test_discord_bot.py -k 'review or mapping' -v`

Expected: FAIL because review components are missing.

- [ ] **Step 3: Build candidate collection and review cards**

Read the raw ingest log and parse each record. Retain only parsed debit records whose payee is absent from `Mappings.payees`. De-duplicate cards by payee. Create one embed per payee with its latest transaction details and optional latitude and longitude.

- [ ] **Step 4: Add mapping components**

Create a `discord.ui.View` with:
- a select menu for the four categories listed in the design
- a custom mapping modal with account and match-pattern fields
- a nearby button only if the selected transaction has coordinates

Use one process-wide `asyncio.Lock` around `upsert_mapping` and `rebuild_journal`. Run the rebuild with `await asyncio.to_thread(...)`. On success, edit the originating embed and remove its view.

- [ ] **Step 5: Add nearby suggestions**

Call the existing `/usr/local/bin/nearby` executable through `asyncio.create_subprocess_exec`. Parse its output lines. Display up to ten suggestions in an ephemeral embed. Selecting a suggestion sets the exact payee mapping key to the selected merchant name only after the user selects a category.

- [ ] **Step 6: Run review tests and full test suite**

Run: `.venv/bin/pytest tests/test_discord_bot.py tests/test_journal.py -v`

Expected: PASS.

- [ ] **Step 7: Commit interactive payee mapping**

```bash
git add finmes/discord_bot.py tests/test_discord_bot.py finmes/journal.py tests/test_journal.py
git commit -m "feat: review and map unknown payees in Discord"
```

### Task 6: Scheduled Digests and Documentation

**Files:**
- Modify: `finmes/discord_bot.py`
- Modify: `tests/test_discord_bot.py`
- Modify: `README.md`
- Modify: `instructions.md`

**Interfaces:**
- Produces `post_weekly_digest()` and `post_month_end_digest()` background tasks.

- [ ] **Step 1: Write failing digest scheduling tests**

```python
def test_next_weekly_digest_uses_monday_at_0800_ist():
    now = datetime(2026, 9, 8, 9, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert next_weekly_digest(now) == datetime(2026, 9, 14, 8, tzinfo=ZoneInfo("Asia/Kolkata"))
```

- [ ] **Step 2: Run tests to verify failure**

Run: `.venv/bin/pytest tests/test_discord_bot.py -k digest -v`

Expected: FAIL because digest scheduling is missing.

- [ ] **Step 3: Implement scheduled digest tasks**

Use `zoneinfo.ZoneInfo("Asia/Kolkata")` and `discord.ext.tasks`. Post the weekly digest each Monday at 08:00 IST. Post the month-end digest on day 1 at 08:00 IST. Use hledger queries for category spending, top payees, and unknown-payee count. Include a `Review Unmapped` button when unknown payees exist.

- [ ] **Step 4: Update documentation**

Document:
- the dedicated `discord-bot` service
- required Discord developer settings: Message Content Intent and application command scope
- `DISCORD_ALLOWED_USERS` and `DISCORD_HOME_CHANNEL`
- supported slash commands and natural-language triggers
- the mapping workflow, including journal rebuild behavior

Remove instructions that state the Hermes gateway starts the Discord adapter.

- [ ] **Step 5: Run documentation and test checks**

Run: `.venv/bin/pytest -v && python3 /Users/unnat-deepsource/.claude/skills/ste-writing/ste-linter.py README.md instructions.md`

Expected: all tests pass and each documentation file has `total_per100w` at or below `3.0`.

- [ ] **Step 6: Commit digests and documentation**

```bash
git add finmes/discord_bot.py tests/test_discord_bot.py README.md instructions.md
git commit -m "feat: add Discord digests"
```

## Plan Self-Review

- **Spec coverage:** Task 1 implements deterministic mapping and true historical rebuilds. Tasks 2 and 4 implement fast finance commands and Hermes routing. Task 5 implements interactive review, mapping, and nearby suggestions. Task 6 implements weekly and month-end digests.
- **Correctness correction:** The original design called a normal replay sufficient for mapping updates. The current replay is deduplicating and cannot update historical entries. Task 1 adds a rebuild that replaces only derived year files after successful generation.
- **Scope boundary:** The plan does not map unparsed SMS records. Such records need parser-template authoring, not a payee category.
- **Placeholders:** No TBD or deferred implementation instructions remain. All cross-task interfaces are named in the task that defines them.
- **Type consistency:** Bot command code consumes only the query, mapping, and rebuild functions defined in Tasks 1 through 3.
