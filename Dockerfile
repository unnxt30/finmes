FROM nousresearch/hermes-agent

# hledger from the upstream release, not Debian's (trixie ships 1.32; the
# finmes-hledger skill file targets 1.52). Upstream publishes no linux-arm64
# binary; the x64 one runs via Rosetta on Apple Silicon Docker Desktop —
# true on both the dev Mac and the always-on Air this deploys to.
ARG HLEDGER_VERSION=1.52.1
RUN curl -sL "https://github.com/simonmichael/hledger/releases/download/${HLEDGER_VERSION}/hledger-linux-x64.tar.gz" \
    | tar xz -C /usr/local/bin hledger

# hledger (GHC) needs a UTF-8 locale to read the ₹ symbol in the journal.
ENV LANG=C.UTF-8 LC_ALL=C.UTF-8

# Agent-callable POI lookup for journal lat:/lon: tags.
COPY scripts/nearby.py /usr/local/bin/nearby
RUN chmod +x /usr/local/bin/nearby

COPY pyproject.toml /app/
COPY finmes/ /app/finmes/
RUN pip install --no-cache-dir /app
