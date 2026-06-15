"""
mcp_worker.py
─────────────
Owns the connection to the MCE MCP server.

Handles:
- OAuth 2.0 + PKCE handshake (browser login once)
- Token caching to oauth_tokens.json
- Auto-detection of expired tokens → clears cache → re-authenticates
- Thread-safe bridges for FastAPI layer
"""

import asyncio
import threading
import json
import time
import webbrowser
from pathlib import Path
from typing import Any, Optional

from pydantic import AnyUrl

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

import http.server
import urllib.parse


TOKEN_FILE = Path("oauth_tokens.json")


def _looks_like_token_error(text: str) -> bool:
    """
    Strict detection for actual MCE token expiry — must match the full
    SOAP fault pattern, not just keyword presence. Avoids false positives
    on tool results that happen to mention "unauthorized" or "401" in
    response data (e.g. a record description, error logs, etc).
    """
    if not text:
        return False
    t = text.lower()
    # Real MCE SOAP fault on expired token looks like:
    #   <faultcode>q0:Security</faultcode><faultstring>Token Expired</faultstring>
    # Match the full pattern only.
    has_soap_security = "faultcode" in t and "q0:security" in t
    has_token_expired = "faultstring" in t and "token expired" in t
    if has_soap_security and has_token_expired:
        return True
    # REST 401 from auth endpoint
    if 'status: 401' in t and ('unauthorized' in t or 'expired' in t):
        return True
    return False


def _clear_tokens():
    """Delete cached tokens so the next connect forces a fresh browser login."""
    if TOKEN_FILE.exists():
        # Keep client_info (dynamic registration) — only drop the tokens
        try:
            data = json.loads(TOKEN_FILE.read_text())
            data.pop("tokens", None)
            TOKEN_FILE.write_text(json.dumps(data, indent=2))
        except Exception:
            TOKEN_FILE.unlink(missing_ok=True)


# ─── Token storage ────────────────────────────────────────────────────────────

class FileTokenStorage(TokenStorage):
    def __init__(self, path: Path = TOKEN_FILE):
        self.path = path

    def _read(self) -> dict:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text())
            except Exception:
                return {}
        return {}

    def _write(self, data: dict) -> None:
        self.path.write_text(json.dumps(data, indent=2))

    async def get_tokens(self) -> Optional[OAuthToken]:
        d = self._read().get("tokens")
        return OAuthToken.model_validate(d) if d else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        d = self._read()
        d["tokens"] = tokens.model_dump(mode="json")
        self._write(d)

    async def get_client_info(self) -> Optional[OAuthClientInformationFull]:
        d = self._read().get("client_info")
        return OAuthClientInformationFull.model_validate(d) if d else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        d = self._read()
        d["client_info"] = client_info.model_dump(mode="json")
        self._write(d)


# ─── OAuth callback catcher ───────────────────────────────────────────────────

class _CallbackCatcher:
    def __init__(self, port: int):
        self.port = port
        self.code: Optional[str] = None
        self.state: Optional[str] = None
        self._server: Optional[http.server.HTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._done = threading.Event()

    def start(self):
        catcher = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass

            def do_GET(self):
                qs = urllib.parse.urlparse(self.path).query
                params = urllib.parse.parse_qs(qs)
                catcher.code = params.get("code", [None])[0]
                catcher.state = params.get("state", [None])[0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(
                    b"<html><body style='font-family:monospace;background:#0a0e14;"
                    b"color:#00ff88;display:flex;align-items:center;justify-content:center;"
                    b"height:100vh;margin:0'><div style='text-align:center'>"
                    b"<h1>&#10003; Authenticated</h1><p style='color:#888'>"
                    b"You can close this tab and return to the app.</p>"
                    b"<script>setTimeout(()=>window.close(),1500)</script>"
                    b"</div></body></html>"
                )
                catcher._done.set()

        self._server = http.server.HTTPServer(("localhost", self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def wait(self, timeout: float = 300) -> tuple[Optional[str], Optional[str]]:
        self._done.wait(timeout)
        if self._server:
            self._server.shutdown()
        return self.code, self.state


# ─── Worker ───────────────────────────────────────────────────────────────────

class MCPWorker:
    def __init__(self, server_url: str, callback_port: int = 3030):
        self.server_url = server_url
        self.callback_port = callback_port
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.session: Optional[ClientSession] = None
        self.status = "idle"
        self.error: Optional[str] = None
        self.tools_cache: list[dict] = []
        self._ready = threading.Event()
        self._stop_evt: Optional[asyncio.Event] = None
        self._token_expired = False   # set when we detect a 500 Token Expired

    def start(self):
        self.thread.start()

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._main())
        except Exception as e:
            self.status = "error"
            self.error = str(e)
            self._ready.set()

    async def _redirect_handler(self, authorization_url: str) -> None:
        self.status = "awaiting_auth"
        self._catcher = _CallbackCatcher(self.callback_port)
        self._catcher.start()
        webbrowser.open(authorization_url)

    async def _callback_handler(self) -> tuple[str, Optional[str]]:
        code, state = await self.loop.run_in_executor(None, self._catcher.wait, 300)
        if not code:
            raise RuntimeError("Timed out waiting for OAuth redirect")
        # Auth code received — login is complete, just establishing session now.
        # Update status so the UI clears "Awaiting login…" promptly instead of
        # waiting until tools/list completes (which can take a few seconds).
        self.status = "connecting"
        return code, state

    async def _main(self):
        self.status = "connecting"
        redirect_uri = f"http://localhost:{self.callback_port}/callback"

        oauth = OAuthClientProvider(
            server_url=self.server_url,
            client_metadata=OAuthClientMetadata(
                client_name="MCE Custom MCP Client",
                redirect_uris=[AnyUrl(redirect_uri)],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                token_endpoint_auth_method="none",
            ),
            storage=FileTokenStorage(),
            redirect_handler=self._redirect_handler,
            callback_handler=self._callback_handler,
        )

        async with streamablehttp_client(self.server_url, auth=oauth) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                self.session = session
                listed = await session.list_tools()
                self.tools_cache = [
                    {
                        "name": t.name,
                        "description": t.description or "",
                        "inputSchema": t.inputSchema or {"type": "object", "properties": {}},
                    }
                    for t in listed.tools
                ]
                self.status = "ready"
                self.error = None
                self._ready.set()
                self._stop_evt = asyncio.Event()
                await self._stop_evt.wait()

    def stop(self):
        """Signal the worker to shut down and stop its event loop."""
        if self._stop_evt:
            try:
                self.loop.call_soon_threadsafe(self._stop_evt.set)
            except Exception:
                pass
        # Give the async cleanup a moment, then stop the loop itself
        def _finalize():
            import time
            time.sleep(0.8)
            try:
                self.loop.call_soon_threadsafe(self.loop.stop)
            except Exception:
                pass
        threading.Thread(target=_finalize, daemon=True).start()

    def _run(self, coro, timeout=120):
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result(timeout=timeout)

    def wait_until_ready(self, timeout=320) -> bool:
        return self._ready.wait(timeout) and self.status == "ready"

    def list_tools(self) -> list[dict]:
        return self.tools_cache

    def call_tool(self, name: str, arguments: dict) -> Any:
        if not self.session:
            raise RuntimeError("MCP session not ready")
        result = self._run(self.session.call_tool(name, arguments))
        out_parts = []
        for block in result.content:
            if getattr(block, "type", None) == "text":
                out_parts.append(block.text)
            else:
                out_parts.append(str(block))
        text = "\n".join(out_parts) if out_parts else ""

        # ── Token expiry detection ──────────────────────────────────────────
        # MCE returns a 500 with faultcode q0:Security / "Token Expired"
        # when the access token has expired. Detect it here and mark the
        # worker so the API layer can surface a clear reconnect message.
        if _looks_like_token_error(text):
            self._token_expired = True
            self.status = "error"
            self.error = "MCE token expired — please reconnect"

        return {
            "isError": bool(getattr(result, "isError", False)),
            "content": text,
            "token_expired": self._token_expired,
        }