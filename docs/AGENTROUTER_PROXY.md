# AgentRouter egress proxy

Render’s Frankfurt IPs (`74.220.51.0/24`, `74.220.59.0/24`) may get **Aliyun WAF HTML** from `agentrouter.org`. Run this tiny proxy on a **home PC or VPS** that AgentRouter does not block, then point the bot at the proxy.

```text
Render bot  →  your proxy (/v1)  →  https://agentrouter.org/v1
                 (+ Codex headers)
```

## 1. Run the proxy

```bash
pip install flask httpx
# optional shared secret
set AGENTROUTER_PROXY_TOKEN=pick-a-long-random-string
set AGENTROUTER_PROXY_PORT=8318
python scripts/agentrouter_proxy.py
```

Health check: `http://127.0.0.1:8318/health`

## 2a. Home PC (Windows) — Cloudflare Tunnel (recommended)

1. Install [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/installation/).
2. Keep the proxy running on port `8318`.
3. Quick tunnel (URL changes on restart):

```powershell
cloudflared tunnel --url http://127.0.0.1:8318
```

4. Copy the `https://….trycloudflare.com` URL.
5. On Render set:

```env
AI_PROVIDER=agentrouter
AGENTROUTER_API_KEY=sk-...
AGENTROUTER_BASE_URL=https://YOUR-SUBDOMAIN.trycloudflare.com/v1
# if you set AGENTROUTER_PROXY_TOKEN on the proxy:
# (dashboard Credentials base URL stays the tunnel; token is only for the proxy)
```

For a **stable** hostname, create a **named** Cloudflare Tunnel (token), then:

1. In [Zero Trust](https://one.dash.cloudflare.com/) → **Networks** → **Tunnels** → your tunnel → **Public Hostname**:
   - Hostname: e.g. `ar-proxy.yourdomain.com`
   - Service: HTTP → `http://host.docker.internal:8318` if cloudflared runs in **Docker on Windows**
   - Or `http://127.0.0.1:8318` if cloudflared runs **natively** (not Docker)
2. Keep `python scripts/agentrouter_proxy.py` on port 8318.
3. Run the connector (do **not** commit the token):

```powershell
docker run -d --name ar-cloudflared --restart unless-stopped `
  cloudflare/cloudflared:latest tunnel --no-autoupdate run --token $env:CLOUDFLARE_TUNNEL_TOKEN
```

4. Render: `AGENTROUTER_BASE_URL=https://ar-proxy.yourdomain.com/v1`

### ngrok alternative

```powershell
ngrok http 8318
```

Then `AGENTROUTER_BASE_URL=https://<ngrok-host>/v1`.

## 2b. Linux VPS

```bash
# on the VPS
sudo apt update && sudo apt install -y python3-pip
pip3 install flask httpx
export AGENTROUTER_PROXY_TOKEN=pick-a-long-random-string
export AGENTROUTER_PROXY_PORT=8318
# put behind nginx + HTTPS, or expose 8318 with a firewall allowlist (Render egress only)
python3 scripts/agentrouter_proxy.py
```

Nginx tip: proxy `https://ar-proxy.example.com` → `http://127.0.0.1:8318`, then:

```env
AGENTROUTER_BASE_URL=https://ar-proxy.example.com/v1
```

Lock down with `AGENTROUTER_PROXY_TOKEN` and/or firewall allowlist of Render CIDRs:

- `74.220.51.0/24`
- `74.220.59.0/24`

## 3. Wire the bot

In Render **Environment** (and optionally dashboard Credentials → AgentRouter base URL):

| Var | Value |
|-----|--------|
| `AI_PROVIDER` | `agentrouter` |
| `AGENTROUTER_API_KEY` | your `sk-…` from [console/token](https://agentrouter.org/console/token) |
| `AGENTROUTER_BASE_URL` | `https://<public-proxy-host>/v1` (**must include `/v1`**) |

Redeploy / restart after changing env. Test Agent **Chat**.

## Security notes

- Prefer `AGENTROUTER_PROXY_TOKEN` so random internet callers cannot burn your AgentRouter quota through an open tunnel.
- If the token is enabled, the bot today only sends the AgentRouter `Authorization` key. Either:
  - leave `AGENTROUTER_PROXY_TOKEN` empty while using a secret Cloudflare/ngrok URL, or
  - put the VPS behind nginx basic-auth / IP allowlist (Render CIDRs above).
- Do not commit API keys or tunnel tokens.

## Other options (if you skip the proxy)

1. Switch Render AI provider to Gemini / OpenAI / Anthropic.
2. Ask AgentRouter Discord to whitelist Render CIDRs.
3. Buy [Render dedicated outbound IPs](https://render.com/docs/dedicated-ips) (~$100/mo) and whitelist those three addresses.
