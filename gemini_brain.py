"""
gemini_brain.py
───────────────
Gemini LLM brain with robust rate-limit handling and
proper (no response) prevention.
"""

import copy
import time
from typing import Callable

from google import genai
from google.genai import types


_UNSUPPORTED_KEYS = {"$schema", "additionalProperties", "$id", "$ref", "definitions", "examples"}


def _sanitize_schema(schema: dict) -> dict:
    if not isinstance(schema, dict):
        return schema
    out = {}
    for k, v in schema.items():
        if k in _UNSUPPORTED_KEYS:
            continue
        if k == "properties" and isinstance(v, dict):
            out[k] = {pk: _sanitize_schema(pv) for pk, pv in v.items()}
        elif k == "items":
            out[k] = _sanitize_schema(v)
        elif isinstance(v, dict):
            out[k] = _sanitize_schema(v)
        else:
            out[k] = v
    if "type" not in out and "properties" in out:
        out["type"] = "object"
    if out.get("type") == "array" and "items" not in out:
        out["items"] = {"type": "string"}
    return out


def _mcp_tools_to_gemini(mcp_tools: list[dict]) -> list[types.Tool]:
    decls = []
    for t in mcp_tools:
        schema = _sanitize_schema(copy.deepcopy(t.get("inputSchema", {})))
        if "type" not in schema:
            schema = {"type": "object", "properties": {}}
        decls.append(
            types.FunctionDeclaration(
                name=t["name"],
                description=t.get("description", ""),
                parameters=schema,
            )
        )
    return [types.Tool(function_declarations=decls)] if decls else []


SYSTEM_INSTRUCTION = (
    "You are an assistant that manages a Salesforce Marketing Cloud Engagement "
    "account through MCP tools. When the user asks you to read, create, or modify "
    "anything in Marketing Cloud, use the available tools. Before any tool call "
    "that creates, updates, or deletes data, briefly explain what you're about to "
    "do. Summarize tool results clearly. If a tool returns an error, explain it "
    "plainly and suggest a fix."
)


def _extract_retry_delay(error_str: str) -> int:
    """Extract retryDelay seconds from a Gemini 429 error string if present."""
    import re
    m = re.search(r"retryDelay['\"]?\s*:\s*['\"]?(\d+)", error_str)
    if m:
        return min(int(m.group(1)) + 2, 65)  # cap at 65s
    return 30  # safe default


def _call_with_retry(client, model, contents, config, emit, max_retries=4):
    """
    Call Gemini with retry on:
      503 UNAVAILABLE  — server overload
      429 RESOURCE_EXHAUSTED — rate limit (respects retryDelay from response)
    """
    for attempt in range(max_retries):
        try:
            resp = client.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
            return resp
        except Exception as e:
            err = str(e)
            is_503 = "503" in err or "UNAVAILABLE" in err
            is_429 = "429" in err or "RESOURCE_EXHAUSTED" in err

            if (is_503 or is_429) and attempt < max_retries - 1:
                if is_429:
                    wait = _extract_retry_delay(err)
                    emit("status", {"text": f"Gemini rate limit hit — waiting {wait}s before retry…"})
                else:
                    wait = 15 * (attempt + 1)
                    emit("status", {"text": f"Gemini busy — retrying in {wait}s…"})
                time.sleep(wait)
            else:
                raise


def _safe_text(resp) -> str:
    """
    Safely extract text from a Gemini response.
    Returns empty string instead of raising if no text parts exist.
    """
    try:
        if resp.text:
            return resp.text
    except Exception:
        pass
    # Fallback: collect text parts manually
    try:
        parts = resp.candidates[0].content.parts or []
        texts = [p.text for p in parts if getattr(p, "text", None)]
        return "\n".join(texts)
    except Exception:
        return ""


class GeminiBrain:
    def __init__(self, api_key: str, model: str = "gemini-2.5-flash"):
        self.client = genai.Client(api_key=api_key)
        self.model = model
        self.history: list[types.Content] = []

    def reset(self):
        self.history = []

    def chat(
        self,
        user_message: str,
        mcp_tools: list[dict],
        call_tool: Callable[[str, dict], dict],
        on_event=None,
    ) -> str:
        def emit(kind, payload):
            if on_event:
                on_event(kind, payload)

        tools = _mcp_tools_to_gemini(mcp_tools)
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            tools=tools or None,
        )

        self.history.append(
            types.Content(role="user", parts=[types.Part.from_text(text=user_message)])
        )

        for round_num in range(12):
            # Small courtesy delay after first round to avoid RPM bursts on free tier
            if round_num > 0:
                time.sleep(2)

            resp = _call_with_retry(self.client, self.model, self.history, config, emit)

            candidate = resp.candidates[0]
            parts = candidate.content.parts or []
            function_calls = [p.function_call for p in parts if getattr(p, "function_call", None)]
            self.history.append(candidate.content)

            if not function_calls:
                text = _safe_text(resp)

                # (no response) prevention — if Gemini returned nothing after a
                # tool call, ask it explicitly to summarise
                if not text and round_num > 0:
                    emit("status", {"text": "Getting summary from Gemini…"})
                    time.sleep(3)
                    self.history.append(
                        types.Content(
                            role="user",
                            parts=[types.Part.from_text(
                                text="Please summarise the tool results above for the user."
                            )]
                        )
                    )
                    try:
                        follow = _call_with_retry(
                            self.client, self.model, self.history, config, emit
                        )
                        text = _safe_text(follow)
                        self.history.append(follow.candidates[0].content)
                    except Exception as e:
                        text = (
                            "The tool executed successfully but I couldn't generate a "
                            f"summary right now due to a rate limit. "
                            f"Please ask me to summarise or try again. ({e})"
                        )

                if not text:
                    text = (
                        "✓ Action completed. "
                        "(Gemini didn't return a summary — likely a free-tier rate limit. "
                        "Ask me what happened or wait a moment and retry.)"
                    )

                emit("final", {"text": text})
                return text

            # Execute tool calls
            response_parts = []
            for fc in function_calls:
                args = dict(fc.args) if fc.args else {}
                emit("tool_call", {"name": fc.name, "args": args})
                try:
                    result = call_tool(fc.name, args)
                except Exception as e:
                    result = {"isError": True, "content": f"Tool execution failed: {e}"}
                emit("tool_result", {"name": fc.name, "result": result})
                response_parts.append(
                    types.Part.from_function_response(
                        name=fc.name,
                        response={
                            "result": result.get("content", ""),
                            "isError": result.get("isError", False),
                        },
                    )
                )
            self.history.append(types.Content(role="user", parts=response_parts))

        emit("final", {"text": "Stopped after too many tool-call rounds."})
        return "Stopped after too many tool-call rounds."
