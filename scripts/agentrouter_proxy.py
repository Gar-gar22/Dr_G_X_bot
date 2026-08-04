#!/usr/bin/env python3
"""
AgentRouter egress proxy — run on a home PC or VPS that is NOT blocked by
Aliyun WAF, then point Render at this proxy:

  AGENTROUTER_BASE_URL=https://<your-tunnel-or-vps>/v1

Forwards OpenAI-compatible paths to https://agentrouter.org and injects
Codex CLI identity headers so the upstream allowlist accepts the request.

Usage:
  pip install flask httpx
  set AGENTROUTER_PROXY_PORT=8318
  python scripts/agentrouter_proxy.py

Optional:
  AGENTROUTER_PROXY_TOKEN=long-random-secret
      Require header X-AgentRouter-Proxy-Token (or ?proxy_token=)
  AGENTROUTER_UPSTREAM=https://agentrouter.org
"""
from __future__ import annotations

import logging
import os
import sys

from flask import Flask, Request, Response, request

try:
    import httpx
except ImportError:
    print("Install httpx: pip install httpx", file=sys.stderr)
    raise

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("agentrouter_proxy")

UPSTREAM = (os.getenv("AGENTROUTER_UPSTREAM") or "https://agentrouter.org").rstrip("/")
PORT = int(os.getenv("AGENTROUTER_PROXY_PORT") or os.getenv("PORT") or "8318")
HOST = os.getenv("AGENTROUTER_PROXY_HOST") or "0.0.0.0"
PROXY_TOKEN = (os.getenv("AGENTROUTER_PROXY_TOKEN") or "").strip()

# Same Codex wire image used by the bot's AgentRouter client.
CODEX_HEADERS = {
    "Originator": "codex_cli_rs",
    "User-Agent": "codex_cli_rs/0.101.0 (Mac OS 26.0.1; arm64) Apple_Terminal/464",
    "Version": "0.101.0",
}

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}

app = Flask(__name__)
_client = httpx.Client(timeout=httpx.Timeout(180.0, connect=30.0), follow_redirects=True)


def _authorized(req: Request) -> bool:
    if not PROXY_TOKEN:
        return True
    header = (req.headers.get("X-AgentRouter-Proxy-Token") or "").strip()
    query = (req.args.get("proxy_token") or "").strip()
    return header == PROXY_TOKEN or query == PROXY_TOKEN


def _forward(path: str):
    if not _authorized(request):
        return Response("unauthorized proxy token", status=401)

    target = f"{UPSTREAM}/{path.lstrip('/')}"
    if request.query_string:
        target = f"{target}?{request.query_string.decode('latin-1')}"

    headers = {}
    for key, value in request.headers:
        lk = key.lower()
        if lk in HOP_BY_HOP or lk == "x-agentrouter-proxy-token":
            continue
        headers[key] = value
    headers.update(CODEX_HEADERS)

    body = request.get_data()
    try:
        upstream = _client.request(
            method=request.method,
            url=target,
            headers=headers,
            content=body,
        )
    except httpx.HTTPError as e:
        logger.error("upstream error %s %s: %s", request.method, target, e)
        return Response(f"proxy upstream error: {e}", status=502)

    out_headers = []
    for key, value in upstream.headers.items():
        if key.lower() in HOP_BY_HOP:
            continue
        out_headers.append((key, value))

    logger.info(
        "%s %s -> %s (%s bytes)",
        request.method,
        path,
        upstream.status_code,
        len(upstream.content),
    )
    return Response(upstream.content, status=upstream.status_code, headers=out_headers)


@app.get("/health")
def health():
    return {"ok": True, "upstream": UPSTREAM}


@app.route("/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
@app.route("/<path:path>", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def catch_all(path: str):
    return _forward(path)


def main() -> None:
    logger.info(
        "AgentRouter proxy listening on %s:%s → %s (token=%s)",
        HOST,
        PORT,
        UPSTREAM,
        "set" if PROXY_TOKEN else "off",
    )
    logger.info("Point Render AGENTROUTER_BASE_URL at https://<public-host>/v1")
    app.run(host=HOST, port=PORT, threaded=True)


if __name__ == "__main__":
    main()
