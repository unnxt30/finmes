# Deploy on a VPS

This deploys the current Hermes-based stack and serves the iPhone ingest endpoint at
`https://finmes.unnat.xyz/ingest`. Caddy obtains and renews the HTTPS
certificate. Hermes stays on the VPS loopback interface; Discord reaches it
through the outbound bot connection.

## 1. Provision the server and DNS

Choose an x86-64 Linux VPS with a public IPv4 address and SSH key login.
4 GB RAM provides comfortable headroom; the small GCP deployment uses 1 GB
with 2 GB swap and needs memory monitoring. Prefer 30 GB disk for image
updates; a 10 GB disk has little spare space. The Dockerfile downloads an x86-64 hledger
binary, so an ARM VPS needs a different build. Allow inbound TCP 22 (SSH),
80 (certificate issuance and HTTPS redirects), and 443 (HTTPS). Do not open
8642 or 8643 in the VPS firewall.

In the DNS settings for `unnat.xyz`, add an **A** record:

| Name | Type | Value |
| --- | --- | --- |
| `finmes` | `A` | the VPS public IPv4 address |

Wait for `finmes.unnat.xyz` to resolve to that address before starting Caddy.
If an `AAAA` record exists for this name, it must point to a working IPv6
address on the VPS; otherwise remove it.

## 2. Install and copy the data

Install Docker Engine and the Compose plugin using the instructions for your
Linux distribution. Clone this repository on the VPS, then clone the private
`finmes-data` repository into `data/`. Give the VPS SSH key read access to
both repositories. The `git@github-personal` host alias in the Mac instructions
is local to that Mac and will not work on a fresh VPS.

```sh
git clone git@github.com:unnxt30/finmes.git
cd finmes
git clone git@github.com:unnxt30/finmes-data.git data
cp .env.example .env
chmod 600 .env
```

Fill in `.env` on the VPS. At minimum, set an OpenRouter inference key, a
fresh random `FINMES_INGEST_TOKEN`, and a fresh random `API_SERVER_KEY`.
Add the Discord variables if using the bot. Keep `.env` and the private data
repository out of the public repo.

If an existing machine has newer `data/` files or Hermes history than the
private data repository, copy them to the VPS before accepting new SMS. The
Hermes history is `.hermes/state.db`.

## 3. Start and verify

```sh
docker compose -f docker-compose.yml -f docker-compose.vps.yml --profile vps up -d --build
docker compose -f docker-compose.yml -f docker-compose.vps.yml --profile vps ps
docker compose -f docker-compose.yml -f docker-compose.vps.yml --profile vps logs --tail=50 caddy ingest hermes
```

Set the Linux host timezone to `Asia/Kolkata`. The VPS override mounts its
timezone into ingestion and Hermes, preserving local receipt-time handling.
On small disks, configure Docker log rotation and monitor `df -h /` before
pulling updated images. Stop the Mac's Hermes container before starting the
VPS bot, and take a final consistent copy of its SQLite databases at cutover.
The current Hermes image runs as UID/GID 10000. After copying Mac state to
Linux, set `.hermes/` ownership recursively to that UID/GID before starting
the bot; copied private subdirectories otherwise remain inaccessible.
Give group 10000 read/traverse access to `data/journal/` (directories 750,
files 640) and verify `hledger check` as container user 10000, not just root.
For the 10 GB deployment, the initial build used
`DOCKER_BUILDKIT=0 docker build -t finmes-hermes .` followed by Compose
`up -d --no-build`, avoiding additional BuildKit cache on the small disk.

Check that HTTPS is active and the ingest path requires authentication:

```sh
curl -i -X POST https://finmes.unnat.xyz/ingest
```

The expected response is `401`. To exercise the full path, send a harmless
non-financial test message with the token from your `.env`:

```sh
curl -i https://finmes.unnat.xyz/ingest \
  -H "Authorization: Bearer YOUR_FINMES_INGEST_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"text":"hello","sender":"ios"}'
```

The expected result is `200` with `status: ignored`; nothing is persisted.
Once verified, change the iPhone shortcut URL from the Mac's tailnet address
to `https://finmes.unnat.xyz/ingest`. Update its token if you generated a new
one. For catch-up, wait for Messages to sync on the Mac, then run there:

```sh
python3 -m finmes.cli recap 2026-10-13 2026-10-18 --url https://finmes.unnat.xyz/ingest
```

The Mac terminal needs Full Disk Access and the matching ingest token in
`.env`. This sends receipt timestamps with their timezone offset and updates
the VPS journal, not the old local copy. Overlapping date ranges are safe.

## Verify GPT-6 Luna through OpenRouter

Hermes is configured with OpenRouter model ID `openai/gpt-6-luna`. It uses the
Chat Completions API and needs function calling to run hledger. OpenAI's
documentation says GPT-6 Luna supports function calling through Chat
Completions only with `reasoning_effort: none`, so `.hermes/config.yaml`
sets:

```yaml
model:
  default: openai/gpt-6-luna
agent:
  reasoning_effort: none
```

After startup, ask a question that must run hledger and verify that a tool
call and a sourced numeric answer appear. Model availability in OpenRouter
alone does not prove compatibility with Hermes's tool loop. If the live check
fails, restore `z-ai/glm-5.3-flash` and `reasoning_effort: medium` in
`.hermes/config.yaml`, then restart Hermes with
`docker compose up -d --force-recreate hermes`.

## Backups

Back up `data/` (especially `ingest.jsonl`, `mappings.toml`, and `journal/`),
`.hermes/state.db`, and Caddy's `caddy_data` volume. Keep backups encrypted
and outside the VPS. The raw SMS log can regenerate journal entries, but it
cannot restore mapping changes or agent history by itself.
