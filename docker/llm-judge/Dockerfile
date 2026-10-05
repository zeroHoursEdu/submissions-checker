FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl python3 python3-docx poppler-utils \
    && rm -rf /var/lib/apt/lists/* && useradd -m -u 10001 judge && mkdir -p /tmp/judge && chown judge /tmp/judge
USER judge
# Pinned: the CLI flags in judgelib.claude_argv were verified against this version.
RUN curl -fsSL https://claude.ai/install.sh | bash -s 2.1.289
ENV PATH="/home/judge/.local/bin:${PATH}" \
    DISABLE_AUTOUPDATER=1 \
    CLAUDE_CONFIG_DIR=/home/judge/.claude
COPY --chown=judge judgelib.py server.py /app/
WORKDIR /app
EXPOSE 8090
CMD ["python3", "/app/server.py"]
