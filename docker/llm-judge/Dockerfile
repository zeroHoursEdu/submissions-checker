FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl python3 python3-docx poppler-utils \
    && rm -rf /var/lib/apt/lists/* && useradd -m -u 10001 judge && mkdir -p /tmp/judge && chown judge /tmp/judge
USER judge
RUN curl -fsSL https://claude.ai/install.sh | bash
ENV PATH="/home/judge/.local/bin:${PATH}"
COPY --chown=judge judgelib.py server.py /app/
WORKDIR /app
EXPOSE 8090
CMD ["python3", "/app/server.py"]
