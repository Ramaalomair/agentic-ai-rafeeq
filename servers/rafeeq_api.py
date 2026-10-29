# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi>=0.150", "uvicorn>=0.40", "openai>=3.30", "pydantic>=2.12", "python-dotenv>=1.1",
#                 "opentelemetry-sdk>=1.45"]
# ///
"""Rafeeq HTTP service with local demo web UI.

Run from repository root:
    AGENT_MODEL=nvidia/nemotron-3.5-lightning:free RAFEEQ_API_KEYS=dev-key-1 \
      uv run servers/rafeeq_api.py --port 8793

The REST endpoint /v1/chat requires X-API-Key. The browser UI at / uses
short-lived in-process session tokens and is restricted to localhost.
Do not expose this demo UI to a public network.
"""
from __future__ import annotations

import argparse
import functools
import json
import os
import secrets
import sqlite3
import sys
import threading
import time
from pathlib import Path

LABS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LABS))

from fastapi import Depends, FastAPI, Header, HTTPException, Request  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from opentelemetry import trace  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

import agentkit as K  # noqa: E402
import madar  # noqa: E402

API_KEYS = {k.strip() for k in os.getenv("RAFEEQ_API_KEYS", "dev-key-1").split(",") if k.strip()}
SESSION_TOKENS = int(os.getenv("RAFEEQ_SESSION_TOKENS", "40000"))
MAX_STEPS = int(os.getenv("RAFEEQ_MAX_STEPS", "6"))
SECOND_MODEL = os.getenv("RAFEEQ_SECOND_MODEL", K.FALLBACKS[0] if K.FALLBACKS else K.MODEL)


class JsonlExporter(SpanExporter):
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def export(self, spans) -> SpanExportResult:
        with self.path.open("a", encoding="utf-8") as f:
            for s in spans:
                f.write(json.dumps({
                    "name": s.name,
                    "trace_id": f"{s.context.trace_id:032x}",
                    "span_id": f"{s.context.span_id:016x}",
                    "parent_id": f"{s.parent.span_id:016x}" if s.parent else None,
                    "ms": round((s.end_time - s.start_time) / 1e6, 1),
                    "attributes": dict(s.attributes or {}),
                }, ensure_ascii=False) + "\n")
        return SpanExportResult.SUCCESS


def tracer_for(path: Path):
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(JsonlExporter(path)))
    return provider.get_tracer("rafeeq")


class TracedShape(K.ChatShape):
    """Chat Completions with one trace span per model call."""

    def __init__(self, tracer, **kw):
        super().__init__(**kw)
        self.tracer = tracer

    def step(self, state, tools, model, use_cache=None, **kw):
        with self.tracer.start_as_current_span(f"chat {model}") as span:
            span.set_attribute("gen_ai.operation.name", "chat")
            span.set_attribute("gen_ai.request.model", model)
            turn = super().step(state, tools, model, use_cache=use_cache, **kw)
            u = turn.usage.model_dump() if hasattr(turn.usage, "model_dump") else (turn.usage or {})
            span.set_attribute("gen_ai.usage.input_tokens", u.get("prompt_tokens") or 0)
            span.set_attribute("gen_ai.usage.output_tokens", u.get("completion_tokens") or 0)
            span.set_attribute("gen_ai.response.finish_reasons", ["tool_calls" if turn.calls else "stop"])
            return turn


def traced(tracer, fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        with tracer.start_as_current_span(f"execute_tool {fn.__name__}") as span:
            span.set_attribute("gen_ai.operation.name", "execute_tool")
            span.set_attribute("gen_ai.tool.name", fn.__name__)
            return fn(*a, **kw)
    return wrapper


class ChatIn(BaseModel):
    session_id: str = Field(min_length=1, max_length=64, pattern=r"^[\w.-]+$")
    message: str = Field(min_length=1, max_length=4000)


class ChatOut(BaseModel):
    session_id: str
    reply: str
    tools: list[str]
    pending_actions: list[dict]
    tokens: dict
    model_steps: int


def create_app(workdir: Path | None = None, trace_file: Path | None = None) -> FastAPI:
    workdir = workdir or K.lab_dir("rafeeq_api")
    m = madar.Madar(workdir, actor="rafeeq-api")
    db = workdir / "sessions.sqlite"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, history TEXT, tokens INTEGER, updated REAL)")
        con.execute("CREATE TABLE IF NOT EXISTS pending_actions (session_id TEXT, tool TEXT, args TEXT, created REAL)")
    tracer = tracer_for(trace_file or K.ARTIFACTS / "traces" / "rafeeq_api.jsonl")
    tools = [traced(tracer, t) for t in m.tools()]
    lock = threading.Lock()
    app = FastAPI(title="Rafeeq API", version="1.0.0")

    # Tokens live only in server memory. Browser access is restricted to local
    # requests; this is deliberately a demo mechanism, not production auth.
    web_sessions: dict[str, float] = {}
    WEB_SESSION_SECONDS = 60 * 60 * 4

    def require_local(request: Request):
        if request.client is None or request.client.host not in {"127.0.0.1", "::1"}:
            raise HTTPException(403, "Local access only")
        # Browsers send an Origin on cross-origin POSTs. Reject them to prevent
        # another website from using the locally running agent.
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "Cross-origin request denied")

    @app.get("/")
    def home(request: Request):
        require_local(request)
        page = LABS / "index.html"
        if not page.is_file():
            raise HTTPException(404, "index.html not found in project root")
        return FileResponse(page, headers={"Cache-Control": "no-store"})

    @app.get("/web/session")
    def create_web_session(request: Request):
        require_local(request)
        now = time.time()
        for token, expiry in list(web_sessions.items()):
            if expiry <= now:
                web_sessions.pop(token, None)
        token = secrets.token_urlsafe(32)
        web_sessions[token] = now + WEB_SESSION_SECONDS
        return {"token": token}

    def api_key(x_api_key: str = Header(default="")) -> str:
        if x_api_key not in API_KEYS:
            raise HTTPException(401, "Missing or invalid X-API-Key")
        return x_api_key

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True, "model": K.MODEL}

    @app.post("/v1/chat", response_model=ChatOut)
    def chat(req: ChatIn, key: str = Depends(api_key)) -> ChatOut:
        with sqlite3.connect(db) as con:
            row = con.execute("SELECT history, tokens FROM sessions WHERE id = ?", (f"{key}:{req.session_id}",)).fetchone()
        history, spent = (json.loads(row[0]), row[1]) if row else ([], 0)
        if spent >= SESSION_TOKENS:
            raise HTTPException(429, f"Session budget of {SESSION_TOKENS} tokens used up. Start a new session.")
        pending: list[dict] = []

        def permission(name: str, args: dict) -> str:
            if m.is_write(name):
                pending.append({"tool": name, "args": args})
                return "deny"
            return "allow"

        with lock, tracer.start_as_current_span("invoke_agent Rafeeq") as span:
            span.set_attribute("gen_ai.operation.name", "invoke_agent")
            span.set_attribute("gen_ai.agent.name", "Rafeeq")
            span.set_attribute("gen_ai.conversation.id", req.session_id)
            before = (K.LEDGER.input_tokens, K.LEDGER.output_tokens)
            kw = dict(system=madar.SYSTEM_PROMPT, shape=TracedShape(tracer), history=history,
                      max_steps=MAX_STEPS, permission=permission, verbose=False)
            try:
                run = K.run_agent(req.message, tools, **kw)
            except Exception as e:
                span.set_attribute("error.type", type(e).__name__)
                run = K.run_agent(req.message, tools, model=SECOND_MODEL, **kw)
            used = {"input": K.LEDGER.input_tokens - before[0],
                    "output": K.LEDGER.output_tokens - before[1]}
        reply = run.final or "I couldn't finish this request. An officer will follow up."
        if pending:
            reply += "\n\n(An officer will review and confirm: " + ", ".join(p["tool"] for p in pending) + ".)"
        with sqlite3.connect(db) as con:
            con.execute("INSERT OR REPLACE INTO sessions VALUES (?,?,?,?)",
                        (f"{key}:{req.session_id}", json.dumps(run.state[1:], ensure_ascii=False, default=str),
                         spent + used["input"] + used["output"], time.time()))
            con.executemany("INSERT INTO pending_actions VALUES (?,?,?,?)",
                            [(req.session_id, p["tool"], json.dumps(p["args"], ensure_ascii=False), time.time())
                             for p in pending])
        return ChatOut(session_id=req.session_id, reply=reply, tools=[n for n, _ in run.tool_calls],
                       pending_actions=pending, tokens=used, model_steps=run.steps)

    @app.post("/web/chat", response_model=ChatOut)
    def web_chat(req: ChatIn, request: Request,
                 token: str = Header(default="", alias="X-Web-Session")) -> ChatOut:
        require_local(request)
        expiry = web_sessions.get(token, 0)
        if not token or expiry <= time.time():
            raise HTTPException(401, "Invalid or expired web session")
        # Keep browser conversations separate from external API clients.
        # The real API key is used only inside the Python process.
        if not API_KEYS:
            raise HTTPException(503, "API key not configured")
        return chat(req, key=sorted(API_KEYS)[0])

    return app


if __name__ == "__main__":
    import uvicorn

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8793)
    args = ap.parse_args()
    uvicorn.run(create_app(), host="127.0.0.1", port=args.port, log_level="warning")
