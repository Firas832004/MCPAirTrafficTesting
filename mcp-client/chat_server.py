"""Chat web app. You type a question; this MCP client fetches the tools from the MCP server and gives them
to Claude with your prompt; Claude picks a tool and arguments; this client runs the tool on the MCP server and
hands the result back to Claude, which writes the answer.
Env: MCP_API_TOKEN (required), MCP_SERVER_URL, CHAT_PORT (default 8002), CLAUDE_MODEL (default claude-haiku-4-5),
     Claude credentials: ANTHROPIC_API_KEY (or an `ant auth login` session) - read by the SDK, never stored here."""
import json, logging, os, sys
from pathlib import Path

import anthropic, uvicorn
from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from connect import call, connect

HERE, MODEL, MAX_STEPS = Path(__file__).parent, os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5"), 6
MAX_MESSAGES, MAX_CHARS, MAX_RESULT_CHARS = 30, 4000, 30000
log = logging.getLogger("chat")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
for noisy in ("httpx2", "httpx", "mcp"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

SYSTEM = """You are an air-traffic assistant. Answer only from tool results; never invent aircraft or values.
If a tool returns an error or nothing, say so plainly. Units: altitude in feet, speed in knots, heading in degrees true.
Rough areas (lat, lon): Saudi Arabia 16-33, 34-56; Riyadh 24.7, 46.7; Jeddah 21.5, 39.2; Dammam 26.4, 50.1 (use a box
around a city). Use get_server_status if asked whether the data is live or demo. Keep answers short; use a compact list
or table for several aircraft."""


async def ask(history: list[dict]) -> dict:
    """Run one user turn: Claude <-> MCP tools until Claude answers. Returns reply text, tool trace and token usage."""
    ai, trace, tokens = anthropic.AsyncAnthropic(), [], {"input": 0, "output": 0}
    async with connect() as mcp:
        tools = [{"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
                 for t in (await mcp.list_tools()).tools]
        messages = list(history)
        for _ in range(MAX_STEPS):
            resp = await ai.messages.create(model=MODEL, max_tokens=2048, system=SYSTEM, tools=tools, messages=messages)
            tokens["input"] += resp.usage.input_tokens
            tokens["output"] += resp.usage.output_tokens
            if resp.stop_reason != "tool_use":
                break
            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                status, data = await call(mcp, block.name, block.input)   # the MCP client runs the tool on the MCP server
                trace.append({"tool": block.name, "args": block.input, "status": status, "result": data})
                results.append({"type": "tool_result", "tool_use_id": block.id, "is_error": status == "error",
                                "content": json.dumps(data)[:MAX_RESULT_CHARS]})
            messages.append({"role": "user", "content": results})
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    return {"reply": text or "I couldn't finish that request. Please try rephrasing.", "tools": trace, "tokens": tokens}


def valid(history) -> bool:
    return (isinstance(history, list) and 0 < len(history) <= MAX_MESSAGES and history[-1].get("role") == "user"
            and all(isinstance(m, dict) and m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)
                    and 0 < len(m["content"]) <= MAX_CHARS for m in history))


async def chat(request):
    if request.headers.get("x-chat-ui") != "1":   # blocks other web pages from making your browser call this
        return JSONResponse({"error": "Forbidden."}, status_code=403)
    try:
        history = (await request.json()).get("messages")
    except ValueError:
        history = None
    if not valid(history):
        return JSONResponse({"error": "Send 1-30 messages (max 4000 characters each), ending with a user message."}, status_code=400)
    try:
        return JSONResponse(await ask(history))
    except Exception as e:
        while getattr(e, "exceptions", None):   # the MCP connection wraps errors in an ExceptionGroup
            e = e.exceptions[0]
        if isinstance(e, anthropic.AuthenticationError) or (isinstance(e, TypeError) and "authentication" in str(e)):
            return JSONResponse({"error": "Claude credentials are missing or invalid. Set ANTHROPIC_API_KEY in the terminal that runs this server."}, status_code=401)
        if isinstance(e, anthropic.RateLimitError):
            return JSONResponse({"error": "Claude is rate limiting requests. Try again shortly."}, status_code=429)
        log.error("chat request failed: %s: %s", type(e).__name__, e)   # details stay in the server console
        return JSONResponse({"error": "Something went wrong. Is the MCP server running and the token correct?"}, status_code=500)


async def page(request):
    return FileResponse(HERE / "chat.html")


app = Starlette(routes=[Route("/", page), Route("/api/chat", chat, methods=["POST"])])

if __name__ == "__main__":
    if not os.environ.get("MCP_API_TOKEN"):
        sys.exit("Set MCP_API_TOKEN to the token the MCP server uses.")
    log.info("chat UI on http://127.0.0.1:%s (model %s)", os.environ.get("CHAT_PORT", "8002"), MODEL)
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("CHAT_PORT", "8002")), log_level="warning")
