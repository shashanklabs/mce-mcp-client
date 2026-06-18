# Marketing Cloud Copilot — Custom MCP Client

A custom [Model Context Protocol (MCP)](https://modelcontextprotocol.io) client for **Salesforce Marketing Cloud Engagement**, with a localhost web chat UI powered by Google Gemini.

> ### You should know
> This is a **proof of concept and learning project** for understanding the Model Context Protocol, OAuth 2.0 PKCE, and LLM function calling against Salesforce MCE. It runs on localhost and makes real Marketing Cloud changes against your live org via authenticated tool calls, with no audit trail or safety net beyond what MCE itself provides. Use it on a sandbox or developer org first, and review every tool call before approving destructive actions.


<img width="1919" height="832" alt="Connected" src="https://github.com/user-attachments/assets/0577fe30-fa28-48b6-b894-0d3ec35f114c" />

## What it does

Type in plain English. The LLM decides which MCE tool to call, executes it through the MCP server, and summarises the result — all in one conversation.

```
You:  "Create a data extension called Leads with Email, FirstName, LastName fields"
App:  → calls sfmc_get_data_extension_folders  (finds the right folder)
      → calls sfmc_create_data_extension        (creates it with your fields)
      ✓ Done. "Leads" data extension created in your Marketing Cloud account.
```

Works for anything the 99 MCE MCP tools support: data extensions, journeys, automations, subscribers, email sends, SMS, events, and more.

## What you'll learn by reading the code

- **The MCP protocol** — how `initialize`, `tools/list`, and `tools/call` work over Streamable HTTP
- **OAuth 2.0 + PKCE from scratch** — why MCE uses PKCE instead of a client secret, how the code verifier and code challenge work
- **LLM function calling** — how MCP tool schemas get converted to Gemini function declarations, how the function-calling loop works
- **MCP client architecture** — separating the MCP layer, LLM layer, and UI so each is independently replaceable
- **MCE Installed Package setup** — how to create a Public App, what scopes are needed, how the MCP server URL is constructed

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  Browser (localhost:8080)                                       │
│  Chat UI — plain English input, collapsible tool call cards     │
└────────────────────┬────────────────────────────────────────────┘
                     │ HTTP
┌────────────────────▼────────────────────────────────────────────┐
│  Python Backend (FastAPI)                                       │
│                                                                 │
│  app.py          → REST API, config management, chat endpoint   │
│  gemini_brain.py → LLM layer (Gemini function-calling loop)     │
│  mcp_worker.py   → MCP session, OAuth 2.0 + PKCE, tool calls   │
└────────────┬───────────────────────────┬────────────────────────┘
             │ Gemini API                │ MCP (Streamable HTTP)
┌────────────▼──────────┐  ┌────────────▼────────────────────────┐
│  Google Gemini        │  │  MCE MCP Server (Salesforce-hosted) │
│  (free API key)       │  │  99 tools, auto-discovered          │
│  Function calling     │  │  OAuth 2.0 + PKCE auth              │
└───────────────────────┘  └─────────────────────────────────────┘
```

**Key design principle:** The LLM is a swappable module. `gemini_brain.py` is the only file that knows about Gemini. Replace it with any LLM that supports function calling and nothing else changes.

---

## Quick Start

**Prerequisites:** Python 3.10+, a Salesforce MCE org, a free Gemini API key

```bash
# 1. Clone
git clone https://github.com/shashanklabs/mce-mcp-client.git
cd mce-mcp-client

# 2. Create virtual environment
py -m venv venv
source venv/Scripts/activate   # Windows (Git Bash)
# source venv/bin/activate     # Mac / Linux

# 3. Install dependencies (5 packages, ~30 seconds)
pip install -r requirements.txt

# 4. Run
py app.py
```

Browser opens at **http://localhost:8080** automatically.

On first launch, the Settings panel opens. Enter:
- Your **MCE MCP Server URL** (from your Installed Package)
- Your **Gemini API Key** (free from [aistudio.google.com/apikey](https://aistudio.google.com/apikey))

Click **Connect** → approve the Marketing Cloud login (one time) → start chatting.

---

## MCE Setup (one-time, ~10 minutes)

You need a **Public App** installed package in Marketing Cloud Engagement.

1. Log into Marketing Cloud → **Setup → Apps → Installed Packages → New**
2. Add an **API Integration** component → choose **Public App** (not Server-to-Server — the MCP server requires PKCE which is only supported by Public Apps)
3. Set OAuth scopes: `offline_access`, `openid` at minimum. Add data/email/automation scopes based on which tools you want to use
4. The app will show you the exact **Redirect URI** to paste back into your Installed Package
5. Your **MCP Server URL** is:
   ```
   # US:
   https://mai-mce-mcp-cdp1.sfdc-yfeipo.svc.sfdcfc.net/t/{TENANT_ID}/c/{CLIENT_ID}/api/mcp

   # EU:
   https://mai-mce-mcp-cdp1.sfdc-yzvdd4.svc.sfdcfc.net/t/{TENANT_ID}/c/{CLIENT_ID}/api/mcp
   ```
   - **Tenant ID**: subdomain of your Auth Base URI (e.g. `https://`**`mcphchq9d5b8`**`.auth.marketingcloudapis.com`)
   - **Client ID**: shown on the Installed Package detail page

> 💡 The `offline_access` scope is what enables the refresh token, which allows the app to silently re-authenticate on restart instead of forcing you to log in every 20 minutes.

---

## How the authentication works

This app uses **OAuth 2.0 Authorization Code + PKCE** — the same standard used by enterprise applications.

- Your MCE username and password are entered directly on Salesforce's login page. They are **never seen or stored** by this app.
- After login, Salesforce issues an `access_token` (short-lived) and `refresh_token` (long-lived), saved to `oauth_tokens.json` locally.
- On every restart, the app silently uses the `refresh_token` to get a new `access_token` — no browser login needed.
- The browser login only appears again when the refresh token expires (typically after weeks of inactivity) or when you click **Reconnect**.
- Every tool call runs with **your** Marketing Cloud user permissions — field-level security and sharing rules apply exactly as if you were logged in.

---

## Features

| Feature | Detail |
|---|---|
| **99 MCE tools** | Auto-discovered from the MCP server on connect — no hardcoding |
| **Uses Gemini free tier** | Google AI Studio free key works for testing and demos |
| **Collapsible tool cards** | See exactly which tools were called and what they returned |
| **Model switcher** | Switch between Gemini 2.5 Flash, Flash Lite, and Pro from the UI |
| **Tool search** | Filter the 99 tools by name or description |
| **Settings panel** | Configure everything from the UI — no `.env` editing required |
| **Token auto-refresh** | Silent re-auth on restart via OAuth refresh token |
| **Rate limit handling** | Auto-retry on Gemini 429/503 with backoff |
| **Conversation memory** | Full context kept across turns in the same session |

---

## Project structure

```
mce-mcp-client/
├── app.py              FastAPI server — REST API, config, chat endpoint
├── gemini_brain.py     Gemini LLM — function-calling loop, tool schema conversion
├── mcp_worker.py       MCP client — OAuth PKCE, session management, tool execution
├── static/
│   └── index.html      Full chat UI (single file, no build step)
├── .env.example        Configuration template
├── .gitignore
├── requirements.txt    5 Python packages
└── LICENSE             MIT
```

---

## Configuration

All configuration is stored in `ui_config.json` (created on first save from the Settings panel). You can also use a `.env` file — copy `.env.example` to `.env` and fill in your values.

| Setting | Where to find it |
|---|---|
| `MCE_MCP_URL` | Constructed from your Installed Package Tenant ID + Client ID |
| `GEMINI_API_KEY` | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) — free |
| `GEMINI_MODEL` | `gemini-2.5-flash` (default), `gemini-2.5-flash-lite`, `gemini-2.5-pro` |
| `OAUTH_CALLBACK_PORT` | Default `3030` — must be free on your machine |
| `APP_PORT` | Default `8080` |

**Files that contain secrets — never commit these:**
- `.env`
- `ui_config.json`
- `oauth_tokens.json`

All three are in `.gitignore` by default.

---

## Security

This app is designed for local development on a trusted machine. Specifically:

- **Localhost-only binding.** The server listens on `127.0.0.1:8080` and rejects any request with a non-localhost `Host` header.
- **CSRF protection.** State-changing requests (POST/PUT/DELETE) with a cross-origin `Origin` or `Referer` are rejected, so a malicious website you happen to have open can't trigger MCE actions through your browser.
- **Secrets stay local.** Your Gemini API key and OAuth tokens never leave your machine. They are stored in `ui_config.json` and `oauth_tokens.json` (both gitignored).
- **API key masking.** The Gemini key is never returned in plaintext from API endpoints — only `••••••` if one is set.
- **No telemetry.** The app doesn't phone home, send analytics, or log anywhere except your terminal.

**What you should still be careful about:**

- **No human-in-the-loop for destructive tool calls.** If the LLM decides to call `sfmc_delete_*` or modify a journey, it executes immediately. Use a sandbox/developer org first, and watch the tool call cards in the chat before approving sensitive actions.
- **Tokens on disk are unencrypted.** `oauth_tokens.json` contains your MCE access and refresh tokens in plain JSON. Anyone with file access to your machine could read them.
- **Don't expose this to the internet.** It's a localhost dev tool. Don't put it behind a tunnel, reverse proxy, or run on a shared/remote machine.
- **PII visibility.** Tool results (including any subscriber email, name, or other personal data MCE returns) are visible in the chat and could be exposed if you screen-share, screenshot, or stream the app. Test with non-production data.

---

## Dependencies

| Package | Version | Purpose |
|---|---|---|
| `mcp` | ≥1.9.0 | MCP Python SDK — protocol, OAuth, Streamable HTTP transport |
| `google-genai` | ≥1.0.0 | Gemini API client — function calling |
| `fastapi` | ≥0.110.0 | Web framework — REST API and static file serving |
| `uvicorn` | ≥0.27.0 | ASGI server |
| `python-dotenv` | ≥1.0.0 | `.env` file loading |

No Node.js. No frontend build step. No database.

---

## Limitations

- **Free tier rate limits:** Gemini's free tier has per-minute request limits. Complex multi-step conversations (e.g. creating a journey) make 3–5 API calls in sequence. If you hit the limit, the app retries automatically with a delay.
- **Local only:** This runs on `localhost` — it's a development/learning tool, not a production deployment.
- **MCE MCP server scope:** Limited to what Salesforce's hosted MCP server exposes. Currently ~99 tools across MCE's REST and SOAP APIs.
- **Session persistence:** Conversation history resets when you restart the app or click Clear.

---

## Contributing

This is a learning project shared openly. Issues and PRs are welcome.

If you build something on top of this or use it as a reference, a mention would be appreciated — but it's not required (MIT licence).

---

## License

MIT License — see [LICENSE](LICENSE)

Copyright (c) 2026 Shashank Kumar

---

## Acknowledgements

- [Salesforce MCE MCP Server](https://developer.salesforce.com/docs/marketing/mce-mcp/guide/mce-mcp-setup.html) — the hosted MCP server this client connects to
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) — handles the protocol, OAuth, and transport
- [Google Gemini](https://aistudio.google.com) — the LLM brain (free tier)
- [Model Context Protocol](https://modelcontextprotocol.io) — the open standard that makes this possible
