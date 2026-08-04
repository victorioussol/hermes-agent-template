# Match the upstream Hermes fix for SQLite's WAL-reset corruption bug. Debian's
# bundled SQLite is affected, so build and checksum a patched shared library.
FROM debian:13.4 AS sqlite_build
ARG SQLITE_AUTOCONF_VERSION=3530400
ARG SQLITE_SHA256=0e9483900e92cd5de8fd48d16bf9200145a61f7fd5be542a5ac81d8a9516eb9c
RUN apt-get -o Acquire::Retries=3 update && \
 apt-get -o Acquire::Retries=3 install -y --no-install-recommends build-essential ca-certificates curl && \
 rm -rf /var/lib/apt/lists/* && \
 (curl -fsSL --retry 1 --retry-all-errors --connect-timeout 15 --max-time 60 \
   -o /tmp/sqlite.tar.gz "https://sqlite.org/2026/sqlite-autoconf-${SQLITE_AUTOCONF_VERSION}.tar.gz" || \
  curl -fsSL --retry 3 --retry-all-errors --connect-timeout 15 --max-time 120 \
   -o /tmp/sqlite.tar.gz "https://sources.buildroot.net/sqlite/sqlite-autoconf-${SQLITE_AUTOCONF_VERSION}.tar.gz") && \
 printf '%s  %s\n' "${SQLITE_SHA256}" /tmp/sqlite.tar.gz > /tmp/sqlite.sha256 && \
 sha256sum -c /tmp/sqlite.sha256 && \
 tar -xzf /tmp/sqlite.tar.gz -C /tmp && \
 cd "/tmp/sqlite-autoconf-${SQLITE_AUTOCONF_VERSION}" && \
 CFLAGS="-O2 -DSQLITE_ENABLE_FTS5 -DSQLITE_ENABLE_RTREE -DSQLITE_ENABLE_COLUMN_METADATA -DSQLITE_ENABLE_UNLOCK_NOTIFY -DSQLITE_ENABLE_DBSTAT_VTAB -DSQLITE_ENABLE_MATH_FUNCTIONS -DSQLITE_ENABLE_PREUPDATE_HOOK -DSQLITE_ENABLE_SESSION -DSQLITE_SECURE_DELETE -DSQLITE_THREADSAFE=1 -DSQLITE_MAX_VARIABLE_NUMBER=250000" \
 ./configure --prefix=/opt/sqlite-fixed --disable-static && \
 make -j"$(nproc)" && make install

FROM ghcr.io/astral-sh/uv:0.11.6-python3.13-trixie@sha256:b3c543b6c4f23a5f2df22866bd7857e5d304b67a564f4feab6ac22044dde719b

# cache-bust: hermes-reliability-20260711

ARG HERMES_REF=v2026.8.3

COPY --from=sqlite_build /opt/sqlite-fixed/lib/libsqlite3.so.3.53.4 /usr/local/lib/
RUN ln -sf libsqlite3.so.3.53.4 /usr/local/lib/libsqlite3.so.0 && \
 ln -sf libsqlite3.so.3.53.4 /usr/local/lib/libsqlite3.so && \
 printf '/usr/local/lib\n' > /etc/ld.so.conf.d/000-sqlite-fixed.conf && \
 ldconfig && \
 python -c "import sqlite3,sys; sys.exit(1) if sqlite3.sqlite_version_info < (3,51,3) else print(sqlite3.sqlite_version)"

RUN apt-get update && \
 apt-get install -y --no-install-recommends curl ca-certificates git tini && \
 curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
 apt-get install -y --no-install-recommends nodejs && \
 rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 --branch ${HERMES_REF} https://github.com/NousResearch/hermes-agent.git /opt/hermes-agent && \
 cd /opt/hermes-agent && \
 uv pip install --system --no-cache -e ".[all,messaging,tts-premium,honcho,bedrock,anthropic,edge-tts,hindsight,vision]" && \
 cd /opt/hermes-agent/web && \
 npm install --silent && \
 npm run build && \
 cd /opt/hermes-agent/ui-tui && \
 npm install --silent --no-fund --no-audit --progress=false && \
 npm run build && \
 rm -rf /opt/hermes-agent/web /opt/hermes-agent/.git /root/.npm

RUN python -c 'from hermes_cli.config import OPTIONAL_ENV_VARS; print("\n".join(sorted(OPTIONAL_ENV_VARS)))' > /opt/hermes-agent/.optional_env_keys

# Environment-specific operating policy. Keep this in the image so every
# Railway rebuild receives the same reviewed self-improvement procedure.
COPY managed-skills/hermes-continuous-improvement /opt/hermes-agent/skills/hermes-continuous-improvement

# Hermes v0.20 managed scope keeps the subscription-backed model policy
# authoritative while leaving unrelated dashboard settings editable.
COPY managed-config.yaml /etc/hermes/config.yaml
RUN chmod 0755 /etc/hermes && chmod 0644 /etc/hermes/config.yaml

# Prevent the dashboard from offering an in-container self-update. Railway
# images must be rebuilt from this pinned source so upgrades remain auditable
# and rollbacks remain possible.
RUN printf 'docker\n' > /opt/hermes-agent/.install_method

COPY requirements.txt /app/requirements.txt
RUN uv pip install --system --no-cache -r /app/requirements.txt

RUN mkdir -p /data/.hermes

COPY server.py /app/server.py
COPY coo_watchdog.py /app/coo_watchdog.py
COPY openrouter_budget_guard.py /app/openrouter_budget_guard.py
COPY templates/ /app/templates/
COPY start.sh /app/start.sh
COPY career_outbox_append.py /app/career_outbox_append.py
RUN chmod +x /app/start.sh

ENV HOME=/data
ENV HERMES_HOME=/data/.hermes
ENV HERMES_TUI_DIR=/opt/hermes-agent/ui-tui
ENV HERMES_MANAGED_DIR=/etc/hermes
ENV LLM_MODEL=gpt-5.6-terra
ENV HERMES_MODEL_PROVIDER=openai-codex

ENTRYPOINT ["/usr/bin/tini", "-g", "--"]
CMD ["/app/start.sh"]
