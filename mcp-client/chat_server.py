"""Chat web app. You type a question in the page; Claude answers using the MCP server's tools.
Two engines (env CHAT_ENGINE):
  claude-code (default) - no API key. Runs Claude Code headless on your normal Claude login; Claude Code is the MCP
                          client (it fetches the tools, Claude picks one, the tool runs on the MCP server, Claude answers).
                          One-time setup: sign the command-line tool in with `claude auth login`.
  api                   - this file is the MCP client: it fetches the tools, gives them and your prompt to Claude through
                          the Anthropic API (ANTHROPIC_API_KEY or an `ant auth login` session), runs the tool Claude
                          picks on the MCP server, and hands the result back to Claude.
Env: MCP_API_TOKEN (required), MCP_SERVER_URL, CHAT_PORT (default 8002), CLAUDE_MODEL (default haiku / claude-haiku-4-5),
     CLAUDE_BIN (path to the claude command-line tool, found automatically if unset)."""
import asyncio, glob, json, logging, os, shutil, sys, tempfile
from pathlib import Path

import anthropic, uvicorn
from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from connect import URL, call, connect

ENGINE = os.environ.get("CHAT_ENGINE", "claude-code")
HERE, MAX_STEPS = Path(__file__).parent, 6
MODEL = os.environ.get("CLAUDE_MODEL") or ("haiku" if ENGINE == "claude-code" else "claude-haiku-4-5")
SERVER_NAME, CLI_TIMEOUT = "air-traffic", 120
MAX_MESSAGES, MAX_CHARS, MAX_RESULT_CHARS = 30, 4000, 30000
log = logging.getLogger("chat")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
for noisy in ("httpx2", "httpx", "mcp"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

def say(label: str, text, limit: int = 400) -> None:
    """One labelled line in the terminal that runs the chat server: the conversation as it happens."""
    text = " ".join(str(text).split())
    print(f"{label:<14}{text[:limit]}{'…' if len(text) > limit else ''}", flush=True)


PAGE, MCPC, CLAUDE = "[CHAT PAGE]", "[MCP CLIENT]", "[CLAUDE]"
SYSTEM = """You are an air-traffic assistant. Answer only from tool results; never invent aircraft or values.
If a tool returns an error or nothing, say so plainly. Units: altitude in feet, speed in knots, heading in degrees true.
Rough areas (lat, lon): Saudi Arabia 16-33, 34-56; Riyadh 24.7, 46.7; Jeddah 21.5, 39.2; Dammam 26.4, 50.1 (use a box
around a city). Use get_server_status if asked whether the data is live or demo. Keep answers short; use a compact list
or table for several aircraft."""


class ChatError(Exception):
    """A problem with a user-facing message."""
    def __init__(self, status: int, message: str):
        self.status, self.message = status, message


def find_cli() -> str:
    """The claude command-line tool: CLAUDE_BIN, then PATH, then the copy bundled with the Claude app."""
    if os.environ.get("CLAUDE_BIN"):
        return os.environ["CLAUDE_BIN"]
    bundled = sorted(glob.glob(os.path.expanduser("~/Library/Application Support/Claude/claude-code/*/claude.app/Contents/MacOS/claude")))
    cli = shutil.which("claude") or (bundled[-1] if bundled else None)
    if not cli:
        raise ChatError(500, "The claude command-line tool was not found. Install Claude Code or set CLAUDE_BIN.")
    return cli


def tool_text(content) -> object:
    """A tool_result's content (text blocks or a string) -> parsed JSON when possible."""
    text = content if isinstance(content, str) else "".join(b.get("text", "") for b in content or [] if isinstance(b, dict))
    try:
        return json.loads(text)
    except ValueError:
        return text


async def ask_claude_code(history: list[dict]) -> dict:
    """Run one turn through headless Claude Code. It acts as the MCP client: it loads the MCP server's tools, Claude
    picks and calls them, and Claude writes the answer. Only this MCP server's tools are allowed (no files, no shell)."""
    cli = find_cli()
    past = "".join(f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['content']}\n" for m in history[:-1])
    prompt = (f"Conversation so far:\n{past}\n" if past else "") + f"User's new message: {history[-1]['content']}"
    mcp_config = json.dumps({"mcpServers": {SERVER_NAME: {"type": "http", "url": URL,
                             "headers": {"Authorization": "Bearer ${MCP_API_TOKEN}"}}}})   # token comes from the environment
    cmd = [cli, "-p", "--model", MODEL, "--output-format", "stream-json", "--verbose", "--max-turns", str(MAX_STEPS + 2),
           "--mcp-config", mcp_config, "--strict-mcp-config", "--tools", "", "--allowedTools", f"mcp__{SERVER_NAME}",
           "--append-system-prompt", SYSTEM, "--no-session-persistence", "--disable-slash-commands"]
    proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE, cwd=tempfile.gettempdir())
    proc.stdin.write(prompt.encode())
    proc.stdin.close()
    err_task = asyncio.create_task(proc.stderr.read())
    trace, pending, state = [], {}, {"final": None, "tokens": {"input": 0, "output": 0}}

    def handle(ev: dict) -> None:
        blocks = (ev.get("message") or {}).get("content")
        for b in blocks if isinstance(blocks, list) else []:
            if ev.get("type") == "assistant" and b.get("type") == "tool_use":
                pending[b["id"]] = {"tool": b["name"].removeprefix(f"mcp__{SERVER_NAME}__"), "args": b.get("input", {}), "status": "ok", "result": None}
                trace.append(pending[b["id"]])
                say(MCPC, f"→ MCP server: call {pending[b['id']]['tool']}({json.dumps(b.get('input', {}))})")
            elif ev.get("type") == "user" and b.get("type") == "tool_result" and b.get("tool_use_id") in pending:
                t = pending[b["tool_use_id"]]
                t["status"], t["result"] = ("error" if b.get("is_error") else "ok"), tool_text(b.get("content"))
                say(MCPC, f"← MCP server result ({t['status']}): {json.dumps(t['result'])}")
        if ev.get("type") == "result":
            state["final"] = ev
            u = ev.get("usage") or {}
            state["tokens"] = {"input": u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0),
                               "output": u.get("output_tokens", 0)}

    async def pump() -> None:
        async for raw in proc.stdout:
            try:
                handle(json.loads(raw))
            except ValueError:
                continue

    try:
        await asyncio.wait_for(pump(), CLI_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        raise ChatError(504, "Claude Code took too long to answer.") from None
    err = (await err_task).decode(errors="replace")
    await proc.wait()
    final, tokens = state["final"], state["tokens"]
    text = ((final or {}).get("result") or "").strip()
    problem = (text + err).lower()
    if final is None or final.get("is_error"):
        if any(w in problem for w in ("authenticate", "oauth", "login", "log in", "credentials", "401")):
            raise ChatError(401, f'Claude Code is not signed in. In a terminal run:  "{cli}" auth login   then try again.')
        log.error("claude-code failed (exit %s): %s", proc.returncode, (text or err)[:300])
        raise ChatError(500, "Claude Code could not answer. Is the MCP server running (check the terminal that started it)?")
    return {"reply": text or "I couldn't finish that request. Please try rephrasing.", "tools": trace, "tokens": tokens}


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
                say(MCPC, f"→ MCP server: call {block.name}({json.dumps(block.input)})")
                status, data = await call(mcp, block.name, block.input)   # the MCP client runs the tool on the MCP server
                say(MCPC, f"← MCP server result ({status}): {json.dumps(data)}")
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
    say(PAGE, f"user asked: {history[-1]['content']}")
    say(PAGE, f"→ Claude ({MODEL}, engine {ENGINE}) with the tools from the MCP server at {URL}")
    try:
        out = await (ask_claude_code(history) if ENGINE == "claude-code" else ask(history))
        say(CLAUDE, f"answer: {out['reply']}")
        say(PAGE, f"done: {len(out['tools'])} tool call(s), {out['tokens']['input']} in / {out['tokens']['output']} out tokens")
        return JSONResponse(out)
    except ChatError as e:
        say(PAGE, f"error: {e.message}")
        return JSONResponse({"error": e.message}, status_code=e.status)
    except Exception as e:
        while getattr(e, "exceptions", None):   # the MCP connection wraps errors in an ExceptionGroup
            e = e.exceptions[0]
        if isinstance(e, anthropic.AuthenticationError) or (isinstance(e, TypeError) and "authentication" in str(e)):
            say(PAGE, "error: Claude credentials are missing or invalid")
            return JSONResponse({"error": "Claude credentials are missing or invalid. Set ANTHROPIC_API_KEY in the terminal that runs this server."}, status_code=401)
        if isinstance(e, anthropic.RateLimitError):
            return JSONResponse({"error": "Claude is rate limiting requests. Try again shortly."}, status_code=429)
        say(PAGE, f"error: {type(e).__name__}: {e}")   # details stay in the server console
        return JSONResponse({"error": "Something went wrong. Is the MCP server running and the token correct?"}, status_code=500)


async def page(request):
    return FileResponse(HERE / "chat.html")


app = Starlette(routes=[Route("/", page), Route("/api/chat", chat, methods=["POST"])])

if __name__ == "__main__":
    if not os.environ.get("MCP_API_TOKEN"):
        sys.exit("Set MCP_API_TOKEN to the token the MCP server uses.")
    log.info("chat UI on http://127.0.0.1:%s (engine %s, model %s)", os.environ.get("CHAT_PORT", "8002"), ENGINE, MODEL)
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("CHAT_PORT", "8002")), log_level="warning")
