FROM python:3.12-slim AS builder

ARG TRADINGAGENTS_COMMIT=be952b8eccb49720509af544c6675233bc1f10d0
ENV PATH="/opt/venv/bin:$PATH" \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && python -m venv /opt/venv

RUN pip install "git+https://github.com/TauricResearch/TradingAgents.git@${TRADINGAGENTS_COMMIT}"

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

FROM python:3.12-slim AS runtime
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    QUANTDESK_HOME=/data \
    QUANTDESK_PLUGIN_SANDBOX=required

RUN apt-get update \
    && apt-get install -y --no-install-recommends bubblewrap ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system quantdesk \
    && useradd --system --gid quantdesk --home-dir /app quantdesk \
    && mkdir -p /data /app \
    && chown -R quantdesk:quantdesk /data /app

COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
USER quantdesk
EXPOSE 8765
CMD ["uvicorn", "quantdesk.api.server:app", "--host", "0.0.0.0", "--port", "8765"]
