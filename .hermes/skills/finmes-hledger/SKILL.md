---
name: finmes-hledger
description: Query the finmes personal-finance hledger journal — balances, spending
  breakdowns, payee analysis, budgets. Use whenever the user asks about their money,
  spending, income, or transactions.
---

# Querying the finmes journal with hledger

The user's finances live in a plain-text hledger journal, generated automatically
from parsed bank SMS (ICICI, SBI, SBI Card). hledger reads it via the `LEDGER_FILE`
environment variable — **never pass `-f`**, just run `hledger <command>`.

Conventions in this journal:

- Primary currency is INR (symbol `₹`). Occasional card spends in USD/SGD.
- Account tree: `assets:bank:icici`, `assets:bank:sbi`, `assets:bank:iob`,
  `liabilities:sbi-card`, `liabilities:icici-card`, `income:salary`,
  `income:cashback`, `expenses:<category>[:<subcategory>]` (e.g.
  `expenses:food:chai`, `expenses:transport:metro`). Uncategorized debit payees
  land in `expenses:unknown`, uncategorized credits in `income:unknown`.
- `assets:transfers` is a wash account: each leg of a transfer between the
  user's own accounts (e.g. paying a credit card bill) posts through it. A
  nonzero balance means one leg's SMS is missing — expected until
  reconciliation, not an error.
- The transaction description IS the payee name. Comments carry tags:
  `ref:` (UPI reference), `key:` (internal dedup id — ignore), `lat:`/`lon:`
  (payment location).
- Sign convention: expense balances are positive, income balances are negative.
  A negative total under `income:` is money earned — flip the sign when reporting.

## Recipes

Adapt these by swapping the account, period, or query terms. Run them exactly as
written (note the quoting).

**What's in my bank accounts?**
`hledger bal assets:bank`

**Overall financial position (assets vs liabilities)?**
`hledger bs`
Net worth is the "Net:" total at the bottom.

**What do I owe on the credit card?**
`hledger bal liabilities:sbi-card`
A negative balance is the outstanding amount owed.

**How much did I spend this month, by category?**
`hledger bal expenses -p thismonth -2 -S`
`-2` collapses to two account levels; `-S` sorts largest first. Drop `-2` for full
subcategory detail.

**Compare spending month over month**
`hledger bal expenses -M -p "last 3 months" -2 -T -A`
One column per month; `-T` adds a row-total column, `-A` a row-average column.

**Income vs expenses / savings rate**
`hledger is -M -p thisyear`
Savings rate = (revenues − expenses) / revenues, computed from the report's totals.

**Top payees this month**
`hledger bal expenses -p thismonth --pivot payee -S -N`
`--pivot payee` groups by payee instead of account; `-N` drops the total row so you
can pipe to `head -10` for a top-10.

**How much on chai / groceries / metro?**
`hledger bal expenses:food:chai -p thismonth`
`acct:` matching is a case-insensitive regex, so `hledger bal chai -p thismonth`
also works if the account name is unique.

**Find a specific transaction (e.g. that Swiggy order last week)**
`hledger print desc:swiggy date:lastweek`
`print` shows the full entry including tags (ref:, lat:/lon:) — best for "find this
transaction" questions.

**Statement view of one account, with running balance**
`hledger aregister assets:bank:icici -p thismonth`

**All transactions above ₹2000 this month**
`hledger reg expenses 'amt:>2000' -p thismonth`
`amt:` compares magnitude (sign ignored) unless you write `amt:'>+2000'`.

**Largest individual expenses this month**
`hledger reg expenses -p thismonth --sort -absamount`
Descending by absolute amount; pipe to `head` for the top few.

**Look up a UPI reference**
`hledger print tag:ref=51834`
Tag values match as infix regex; use the full ref for an exact hit.

**What's uncategorized, and from whom?**
`hledger bal expenses:unknown -p thismonth --pivot payee -S -N`

**Foreign-currency card spends**
`hledger reg cur:USD` — postings in USD. To restate a report in INR:
`hledger bal expenses -p thismonth -X INR --infer-market-prices`
(`-X INR` converts using known market prices; `--infer-market-prices` derives them
from transactions recorded with costs. If no price is known, amounts stay unconverted.)

**Subscription / autopay audit**
`hledger reg desc:netflix -M`
Monthly summary for one payee — a steady amount every month confirms a subscription.
To hunt for candidates: `hledger bal expenses -M -p "last 6 months" --pivot payee -N`
and look for payees charging the same amount each month.

**Budget vs actual**
`hledger bal -M --budget -p thismonth`
Budget goals come from periodic transaction rules in the journal, e.g.
`~ monthly` followed by `  (expenses:food)  ₹8000` postings (written via the finmes
CLI, not by you). Accounts without goals are hidden by default.

**Daily spend this week**
`hledger bal expenses -D -p thisweek -1`

**Journal sanity check (date span, txn count, commodities)**
`hledger stats` — run this first if other results look wrong or empty.
`hledger commodities` shows the exact currency symbols in use (₹ vs INR matters
for `cur:`).

## Syntax reference (only what the recipes use)

Query terms (positional args, combined with flags):

- `acct:REGEX` — match account names (bare terms like `expenses:food` are `acct:`).
- `desc:REGEX` — match transaction descriptions (= payee names here).
- `date:PERIODEXPR` — e.g. `date:thismonth`, `date:2026`, `date:2026-01..2026-03`.
- `amt:N`, `amt:'<N'`, `amt:'>N'`, `amt:'<=N'`, `amt:'>=N'` — posting amounts, not
  balances. Compares absolute magnitude unless N has an explicit `+`/`-` sign.
- `cur:REGEX` — commodity symbol, matched as a FULL regex match (not infix).
- `tag:NAME` or `tag:NAME=VALUE` — both sides are infix regexes.
- `not:TERM` — negate any term, e.g. `not:acct:expenses:unknown`.
- All regexes are case-insensitive. Multiple terms of the same type OR together;
  different types AND together.

Periods — `-p EXPR` or `date:EXPR`, using smart dates:

- Fixed: `-p 2026`, `-p 2026-07`, `-p 2026q1`, `-p "2026-01-15..2026-02-10"`
- Relative: `thismonth`, `lastmonth`, `thisweek`, `lastweek`, `today`, `yesterday`,
  `thisquarter`, `thisyear`, `"last 3 months"`, `"from 2026-04"`, `"to 2026-07"`
- `-b DATE` / `-e DATE` set begin/end individually (end is exclusive).

Flags used above:

- Intervals: `-D` daily, `-W` weekly, `-M` monthly, `-Q` quarterly, `-Y` yearly.
- `-NUM` (e.g. `-1`, `-2`) or `--depth NUM` — collapse accounts deeper than NUM.
- `-S` — balance report sorted by amount, largest first. `-T` row totals, `-A` row
  averages, `-N` no grand-total row, `-t` tree view, `-l` flat (default).
- `--pivot FIELD` — group by `payee`, `desc`, or a tag name instead of account.
- `register --sort FIELDS` — `date`, `desc`, `account`, `amount`, `absamount`;
  prefix with `-` for descending.
- `-X COMM` — convert amounts to commodity COMM; `--infer-market-prices` lets it
  use transaction costs as prices. (`-V` exists but can pick surprising target
  commodities; prefer `-X INR`.)
- `--budget` — on `bal`, show actual vs goals from `~` periodic rules.

Commands: `bal`/`balance`, `reg`/`register`, `aregister`, `print`, `bs`, `is`,
`stats`, `commodities`, `payees`.

## Pitfalls

- **Quote query terms in the shell.** `amt:>2000` unquoted is a shell redirect —
  it will silently create a file. Always `'amt:>2000'`. Same for terms containing
  spaces: `'desc:coffee day'`.
- **`-p` and `date:` intersect.** Mixing them (or two `date:` terms) gives the
  overlap of the periods, which is easily empty. Use one date mechanism per command.
- **Depth changes grouping, not filtering.** `-2` sums subaccounts into their
  parents; the total is unchanged. Without it, flat mode lists every leaf account
  with full names.
- **`amt:` is per-posting and unsigned by default.** `'amt:>2000'` matches a ₹2500
  expense AND a ₹2500 income posting. It never matches account balances.
- **`cur:` needs the exact symbol, fully matched.** Check `hledger commodities`
  first: if the journal uses `₹`, then `cur:₹` (quote it); if `INR`, then `cur:INR`.
  `cur:` does NOT do infix matching, and `$` would need escaping (`cur:'\$'`).
- **`-S` on raw `income:` accounts sorts backwards** (they're negative). Use
  `hledger is -S` instead, which flips signs automatically.
- **Piping `bal` to `head` can eat the total row** — add `-N` and state totals from
  a separate un-truncated run.

## Guardrails (non-negotiable)

- **The journal is READ-ONLY to you.** Never edit, append to, reformat, or delete
  the journal file, and never run hledger commands that write or modify data
  (`add`, `import`, `rewrite`, `close`, shell redirects into the journal). All
  writes happen through the separate finmes CLI only.
- **Never fabricate numbers.** Every figure you report must come verbatim from
  hledger output produced in this session. No estimating, no arithmetic on numbers
  you didn't retrieve, no filling gaps from memory.
- **If a command errors or returns nothing, say so.** Show the actual error or note
  the empty result, then adjust the query. Never substitute a guess for output you
  don't have.

## Payment locations

Entries ingested from the phone carry `lat:`/`lon:` tags (payment-time GPS).
Historical entries and online payments have none.

- Find them: `hledger print tag:lat` (or check any entry's comment).
- Resolve to nearby shops: `nearby <lat> <lon>` (optional radius in metres,
  default 150). Prints up to 10 places, nearest first, plus the locality.
- Use when identifying an unknown payee: show the POI candidates alongside
  the amount/date and ASK the user — never assign a payee mapping from
  location alone.
- If `nearby` fails or an entry has no tags, say so plainly. Do not guess
  merchants from coordinates.

