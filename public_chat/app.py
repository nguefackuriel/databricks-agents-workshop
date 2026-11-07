"""Northline Cycles public chat.

A small Gradio app that anyone can open without a Databricks account. It forwards each
question to a Databricks Genie Agent, server-side, with credentials stored as secrets.
Visitors never see the token.

By default it uses Genie *Agent mode* (multi-step reasoning, tools, a written answer),
the same mode as the Agent toggle in the Genie UI. If Agent mode is not enabled for the
workspace, it falls back to *Chat mode* (one SQL query, results as a table).

Configuration (Hugging Face Space: Settings -> Variables and secrets):
  DATABRICKS_HOST    variable  e.g. https://dbc-1234abcd-5678.cloud.databricks.com
  GENIE_SPACE_ID     variable  the Genie Agent ID (Configure -> About)
  DATABRICKS_TOKEN   secret    a token for a user or service principal that can run the agent
Optional:
  GENIE_MODE                 agent (default) or chat
  MAX_MESSAGES_PER_SESSION   default 15
  MAX_MESSAGES_PER_DAY       default 300 (protects your Free Edition daily quota)
"""
import datetime as dt
import json
import os
import re
import time
from urllib.parse import urlsplit

import gradio as gr
import requests

try:
    # On Hugging Face "ZeroGPU" hardware, a Space must declare at least one GPU
    # function or it refuses to start. This app never uses a GPU and never calls it.
    import spaces

    @spaces.GPU
    def _zerogpu_placeholder():
        return None
except ImportError:  # CPU hardware or local run: nothing to declare
    pass


def _workspace_root(url):
    """Keep only https://<workspace-host>, even if a full browser URL was pasted."""
    url = (url or "").strip()
    if url and "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.netloc else ""


HOST = _workspace_root(os.environ.get("DATABRICKS_HOST", ""))
TOKEN = os.environ.get("DATABRICKS_TOKEN", "").strip()
SPACE_ID = os.environ.get("GENIE_SPACE_ID", "").strip()
GENIE_MODE = os.environ.get("GENIE_MODE", "agent").strip().lower()
GUARDRAILS = os.environ.get("GUARDRAILS", "on").strip().lower() != "off"
MAX_PER_SESSION = int(os.environ.get("MAX_MESSAGES_PER_SESSION", "15"))
MAX_PER_DAY = int(os.environ.get("MAX_MESSAGES_PER_DAY", "300"))
MAX_CHARS = 500
MAX_WAIT_SECONDS = 180
MAX_ROWS_SHOWN = 15
FINAL_STATUSES = {"COMPLETED", "FAILED", "CANCELLED", "QUERY_RESULT_EXPIRED"}

EXAMPLES = [
    "Can order ORD-30004 be returned?",
    "How long is the warranty on an e-bike battery?",
    "Maya Chen wants to return order ORD-30001. How much will she get back?",
    "How long does shipping take to Vancouver?",
]

_daily = {"date": None, "count": 0}


class GenieError(Exception):
    pass


class AgentModeUnavailable(GenieError):
    pass


def _headers():
    return {"Authorization": f"Bearer {TOKEN}"}


# --------------------------------------------------------------------------- formatting

def _cell(value):
    return "" if value is None else str(value).replace("|", "\\|").replace("\n", " ")


def _markdown_table(columns, rows):
    if not columns or not rows:
        return ""
    if len(rows) == 1 and len(columns) > 4:  # one record: easier to read as a list
        return "\n".join(f"- **{c}**: {_cell(v)}" for c, v in zip(columns, rows[0]) if v not in (None, ""))
    shown = rows[:MAX_ROWS_SHOWN]
    lines = ["| " + " | ".join(columns) + " |", "| " + " | ".join("---" for _ in columns) + " |"]
    lines += ["| " + " | ".join(_cell(v) for v in row) + " |" for row in shown]
    if len(rows) > len(shown):
        lines.append(f"\n*Showing {len(shown)} of {len(rows)} rows.*")
    return "\n".join(lines)


def _table_from_chunk(chunk):
    """Best-effort rendering of a table chunk inside an Agent mode message."""
    cols = chunk.get("columns") or chunk.get("column_metadata") or chunk.get("schema") or []
    if isinstance(cols, dict):
        cols = cols.get("columns") or []
    names = [c.get("name", "") if isinstance(c, dict) else str(c) for c in cols]
    rows = chunk.get("preview_rows") or chunk.get("rows") or chunk.get("data_array") or []
    if rows and isinstance(rows[0], dict):
        names = names or list(rows[0].keys())
        rows = [[r.get(n) for n in names] for r in rows]
    return _markdown_table(names, rows)


# --------------------------------------------------------------------------- agent mode

def _sse_events(resp):
    """Parse a Server-Sent Events stream into JSON dicts (adds 'type' from 'event:')."""
    event_name, data_lines = None, []

    def flush():
        payload = "\n".join(data_lines).strip()
        if not payload or payload == "[DONE]":
            return None
        try:
            obj = json.loads(payload)
        except ValueError:
            return None
        if isinstance(obj, dict) and event_name and "type" not in obj:
            obj["type"] = event_name
        return obj

    for raw in resp.iter_lines(decode_unicode=True):
        line = (raw or "").rstrip("\r")
        if line == "":
            obj = flush()
            event_name, data_lines = None, []
            if obj is not None:
                yield obj
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            event_name = value
        elif field == "data":
            data_lines.append(value)
    obj = flush()
    if obj is not None:
        yield obj


_PLACEHOLDER = re.compile(r"^\s*[*_]*\[(?:query results?|table|chart|visuali[sz]ation)\][*_]*\s*$", re.I | re.M)
# Citation markers in any form: [1], [[1]], \[1\], \[[1]\], [1](link), \[[1](link)\], [1, 2]
_CITATION = re.compile(r"[ \t]?(?:\\?\[)+\d+(?:\s*,\s*\d+)*(?:\\?\])+(?:\([^)]*\))?(?:\\?\])*")
_HEADING = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", re.M)


def _clean_agent_text(text):
    """Drop citation markers and placeholders that point to Genie UI elements, shrink headings."""
    text = _PLACEHOLDER.sub("", text)
    text = _CITATION.sub("", text)
    text = _HEADING.sub(r"**\1**", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
_TITLE = re.compile(r"^\s*(#{1,6}\s+.+|\*\*[^*]+\*\*:?|__[^_]+__)\s*$")


def _strip_tables(text, max_rows=5):
    """Genie writes its answer in prose, then may append the raw tool results as markdown tables
    (one wide row per tool call). Drop those small tables and their titles when prose remains.
    Larger tables are kept, so a leak still shows when GUARDRAILS=off."""
    lines = text.split("\n")
    out, i, removed = [], 0, False
    while i < len(lines):
        if lines[i].lstrip().startswith("|") and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]):
            start = i
            i += 2
            while i < len(lines) and lines[i].strip():  # rows, including cells that span lines
                i += 1
            if sum(1 for l in lines[start + 2:i] if l.lstrip().startswith("|")) > max_rows:
                out.extend(lines[start:i])
                continue
            j = len(out)
            while j > 0 and not out[j - 1].strip():
                j -= 1
            if j > 0 and _TITLE.match(out[j - 1]):
                del out[j - 1:]
            removed = True
            continue
        out.append(lines[i])
        i += 1
    rest = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    return rest if removed and rest else text


def _agent_answer(output):
    """The assistant's final report: assistant messages after the last tool result."""
    last_tool = max((i for i, it in enumerate(output) if it.get("type") == "function_call_output"), default=-1)
    messages = [it for i, it in enumerate(output)
                if it.get("type") == "message" and it.get("role") == "assistant" and i > last_tool]
    if not messages:
        messages = [it for it in output if it.get("type") == "message" and it.get("role") == "assistant"]
    parts = []
    for msg in messages:
        content = msg.get("content")
        for chunk in content if isinstance(content, list) else [content]:
            if isinstance(chunk, str):
                parts.append(_clean_agent_text(chunk))
            elif isinstance(chunk, dict):
                if isinstance(chunk.get("text"), str):
                    parts.append(_clean_agent_text(chunk["text"]))
                else:
                    table = _table_from_chunk(chunk)
                    if table:
                        parts.append(table)
    return "\n\n".join(p for p in parts if p and p.strip())


def ask_agent(question, conversation_id):
    """Agent mode. Yields ('status', text) while working, then ('final', conversation_id, answer)."""
    body = {"input": [{"type": "message", "role": "user",
                       "content": [{"type": "input_text", "text": question}]}]}
    if conversation_id:
        body["conversation_id"] = conversation_id
    url = f"{HOST}/api/2.0/genie/agents/{SPACE_ID}/responses"
    started, items, order, final = time.time(), {}, [], None

    with requests.post(url, headers={**_headers(), "Accept": "text/event-stream"}, json=body,
                       stream=True, timeout=(15, MAX_WAIT_SECONDS)) as resp:
        if resp.status_code >= 400:
            detail = resp.text[:500]
            print(f"Agent mode -> {resp.status_code}: {detail}")
            if resp.status_code == 404 and "FEATURE_DISABLED" in detail:
                raise AgentModeUnavailable(detail)
            raise GenieError(resp.status_code)
        resp.encoding = "utf-8"  # text/event-stream has no charset: requests would assume Latin-1
        for event in _sse_events(resp):
            etype = event.get("type", "")
            response = event.get("response") or {}
            conversation_id = response.get("conversation_id") or conversation_id
            if etype.startswith("response.output_item"):
                item = event.get("item") or {}
                key = item.get("id") or f"#{event.get('output_index', len(order))}"
                if key not in items:
                    order.append(key)
                    if item.get("type") == "function_call":
                        yield ("status", "Querying the data...")
                    elif item.get("type") == "reasoning":
                        yield ("status", "Thinking...")
                items[key] = item
            elif etype in ("response.completed", "response.failed"):
                final = response
                break
            if time.time() - started > MAX_WAIT_SECONDS:
                yield ("final", conversation_id, "Sorry, this is taking too long. Please try again in a moment.")
                return

    if final is not None and final.get("status") == "failed":
        print(f"Agent mode failed: {final.get('error')}")
        yield ("final", conversation_id, "Sorry, I could not answer that one. Try rephrasing your question.")
        return
    output = (final or {}).get("output") or [items[k] for k in order]
    answer = _agent_answer(output)
    yield ("final", conversation_id,
           answer or "I could not find an answer to that. Try rephrasing your question.")


# --------------------------------------------------------------------------- chat mode

def _api(method, path, **kwargs):
    url = f"{HOST}/api/2.0/genie/spaces/{SPACE_ID}{path}"
    resp = requests.request(method, url, headers=_headers(), timeout=30, **kwargs)
    if resp.status_code >= 400:
        print(f"Genie API {method} {path} -> {resp.status_code}: {resp.text[:500]}")
        raise GenieError(resp.status_code)
    return resp.json()


def _ids_from_start(data):
    conversation_id = data.get("conversation_id") or (data.get("conversation") or {}).get("id")
    message = data.get("message") or {}
    message_id = data.get("message_id") or message.get("message_id") or message.get("id")
    if not conversation_id or not message_id:
        raise GenieError("unexpected start-conversation response")
    return conversation_id, message_id


def _format_chat_answer(conversation_id, message_id, message):
    texts, parts, suggestions = [], [], []
    for att in message.get("attachments") or []:
        if att.get("text") and att["text"].get("content"):
            texts.append(att["text"]["content"])
        if att.get("query"):
            query = att["query"]
            if query.get("description"):
                parts.append(query["description"])
            attachment_id = att.get("attachment_id") or att.get("id") or query.get("id")
            if attachment_id:
                try:
                    result = _api("GET", f"/conversations/{conversation_id}/messages/{message_id}"
                                         f"/attachments/{attachment_id}/query-result")
                    sr = result.get("statement_response") or {}
                    columns = [c.get("name", "") for c in
                               ((sr.get("manifest") or {}).get("schema") or {}).get("columns", [])]
                    table = _markdown_table(columns, (sr.get("result") or {}).get("data_array") or [])
                    if table:
                        parts.append(table)
                except GenieError:
                    parts.append("*The result table could not be loaded.*")
        if att.get("suggested_questions"):
            suggestions += att["suggested_questions"].get("questions") or []
    parts = texts + parts
    if suggestions:
        parts.append("**You could also ask:**\n" + "\n".join(f"- {q}" for q in suggestions[:3]))
    return "\n\n".join(parts) or "I could not find an answer to that. Try rephrasing your question."


def ask_chat(question, conversation_id):
    """Chat mode. Same interface as ask_agent."""
    if conversation_id:
        msg = _api("POST", f"/conversations/{conversation_id}/messages", json={"content": question})
        message_id = msg.get("message_id") or msg.get("id")
    else:
        conversation_id, message_id = _ids_from_start(
            _api("POST", "/start-conversation", json={"content": question}))

    started, delay = time.time(), 1.0
    while True:
        message = _api("GET", f"/conversations/{conversation_id}/messages/{message_id}")
        status = message.get("status")
        if status in FINAL_STATUSES:
            break
        if time.time() - started > MAX_WAIT_SECONDS:
            yield ("final", conversation_id, "Sorry, this is taking too long. Please try again in a moment.")
            return
        time.sleep(delay)
        delay = min(delay * 1.5, 5.0)

    if status != "COMPLETED":
        print(f"Genie message {message_id} ended with {status}: {message.get('error')}")
        yield ("final", conversation_id, "Sorry, I could not answer that one. Try rephrasing your question.")
        return
    yield ("final", conversation_id, _format_chat_answer(conversation_id, message_id, message))


# --------------------------------------------------------------------------- guardrails
# Two checks around every Genie call. They are heuristics, not a security boundary:
# the real boundary is what the agent's sources let it reach. Set GUARDRAILS=off
# to show the difference in a demo.

REFUSAL = ("I can only help with one specific order or customer, or with our return, warranty, "
           "shipping and rewards policies. For example: *Can order ORD-30004 be returned?*")
WITHHELD = ("Sorry, that answer contained more customer or business data than I can share in this "
            "public chat. Try asking about one specific order, for example: *Can order ORD-30004 be returned?*")

_ORDER_ID = re.compile(r"\bORD-\d{3,}\b", re.I)
_CUSTOMER_ID = re.compile(r"\bC\d{4}\b")
_INJECTION = re.compile(
    r"\b(ignore|disregard|forget|override|bypass)\b.{0,40}\b(instruction|rule|prompt|guardrail|previous|above|system)"
    r"|\bsystem prompt\b|\byou are now\b|\bjailbreak\b|\bdeveloper mode\b|\bpretend (to be|you)\b", re.I)
_BUSINESS = re.compile(
    r"\b(revenue|turnover|profit|margin|sales (figures|numbers|total)|total sales|best[- ]selling|"
    r"how many (customers|clients)|number of (customers|clients))\b", re.I)
# An order count is fine for one customer ("How many orders has C1042 placed?"), not store-wide.
_ORDER_COUNT = re.compile(r"\b(how many orders|number of orders|total orders)\b", re.I)
_BULK = re.compile(
    r"\b(list|show|give|export|dump|download|print|display|name)\b.{0,40}\b(all|every|each|everyone|"
    r"customers|clients|users|people|emails?|addresses)\b|\b(all|every) (customers?|clients?|orders?)\b"
    r"|\bselect\b.+\bfrom\b|\bsql\b", re.I)
_IN_SCOPE = re.compile(
    r"\b(return|refund|warranty|guarantee|ship|shipping|deliver|delivery|rewards?|points?|tier|gold|silver|"
    r"bronze|policy|policies|exchange|store credit|defect|broken|damaged|helmet|battery|e-?bike|bike|order)\w*",
    re.I)


def check_question(question, in_conversation):
    """Input guardrail. Returns a reason string if the question must be refused."""
    has_id = bool(_ORDER_ID.search(question) or _CUSTOMER_ID.search(question))
    if _INJECTION.search(question):
        return "prompt injection"
    if _BUSINESS.search(question) or (_ORDER_COUNT.search(question) and not has_id):
        return "business data"
    if _BULK.search(question) and not has_id:
        return "bulk request"
    if not in_conversation and not has_id and not _IN_SCOPE.search(question):
        return "off topic"
    return None


def check_answer(question, answer):
    """Output guardrail. Returns a reason string if the answer must not be shown."""
    asked = {m.upper() for m in _CUSTOMER_ID.findall(question) + _ORDER_ID.findall(question)}
    customers = {m.upper() for m in _CUSTOMER_ID.findall(answer)} - asked
    orders = {m.upper() for m in _ORDER_ID.findall(answer)} - asked
    table_rows = sum(1 for line in answer.splitlines()
                     if line.strip().startswith("|") and not set(line.strip()) <= set("|-: "))
    if len(customers) > 1:
        return f"{len(customers)} other customers in the answer"
    if len(orders) > 3:
        return f"{len(orders)} other orders in the answer"
    if table_rows > 6:  # header + 5 rows
        return f"table with {table_rows - 1} rows"
    if _BUSINESS.search(answer):
        return "business data in the answer"
    return None


# --------------------------------------------------------------------------- UI

def _daily_quota_left():
    today = dt.date.today()
    if _daily["date"] != today:
        _daily.update(date=today, count=0)
    return _daily["count"] < MAX_PER_DAY


def _run(question, session):
    """Yield status updates, then the final answer, switching to chat mode if needed."""
    if session["mode"] == "agent":
        try:
            for event in ask_agent(question, session["conversation_id"]):
                if event[0] == "final":
                    session["conversation_id"] = event[1]
                    yield ("final", event[2])
                else:
                    yield event
            return
        except AgentModeUnavailable:
            print("Agent mode is not enabled for this workspace: switching to chat mode.")
            session.update(mode="chat", conversation_id=None)
    for event in ask_chat(question, session["conversation_id"]):
        if event[0] == "final":
            session["conversation_id"] = event[1]
            yield ("final", event[2])


def respond(question, history, session):
    question = (question or "").strip()
    history = history or []
    if not question:
        yield "", history, session
        return
    history = history + [{"role": "user", "content": question}]

    if not (HOST and TOKEN and SPACE_ID):
        reply = "This demo is not configured yet: set DATABRICKS_HOST, GENIE_SPACE_ID and DATABRICKS_TOKEN."
    elif len(question) > MAX_CHARS:
        reply = f"Please keep questions under {MAX_CHARS} characters."
    elif session["count"] >= MAX_PER_SESSION:
        reply = "You have reached the question limit for this demo session. Reload the page to start over."
    elif not _daily_quota_left():
        reply = "The demo has reached its daily limit. Please come back tomorrow."
    elif GUARDRAILS and (reason := check_question(question, bool(session["conversation_id"]))):
        print(f"[guardrail] question blocked ({reason}): {question[:120]!r}")
        session["count"] += 1
        reply = REFUSAL
    else:
        yield "", history + [{"role": "assistant", "content": "Looking that up..."}], session
        reply = "Sorry, the assistant is unavailable right now. Please try again in a moment."
        try:
            for kind, text in _run(question, session):
                if kind == "final":
                    reply = text
                    session["count"] += 1
                    _daily["count"] += 1
                    if GUARDRAILS and (reason := check_answer(question, text)):
                        print(f"[guardrail] answer withheld ({reason}) for: {question[:120]!r}")
                        reply = WITHHELD
                    elif session["mode"] == "agent":
                        reply = _strip_tables(text)  # the guardrail saw the full answer first
                else:
                    yield "", history + [{"role": "assistant", "content": text}], session
        except (GenieError, requests.RequestException) as exc:
            print(f"Error: {exc!r}")
    yield "", history + [{"role": "assistant", "content": reply}], session


def new_session():
    return {"conversation_id": None, "count": 0, "mode": "chat" if GENIE_MODE == "chat" else "agent"}


with gr.Blocks(title="Northline Cycles assistant") as demo:
    gr.Markdown(
        "### Northline Cycles customer care\n"
        "Ask about an order, a return, the warranty or shipping. "
        "Demo on synthetic data, built with a Databricks Genie Agent."
    )
    chatbot = gr.Chatbot(height=460, show_label=False)
    box = gr.Textbox(placeholder="e.g. Can order ORD-30004 be returned?", show_label=False, max_lines=3)
    gr.Examples(EXAMPLES, inputs=box)
    session = gr.State(new_session())
    # private: only this page can call it, not the Gradio API clients ("Use via API")
    box.submit(respond, [box, chatbot, session], [box, chatbot, session], api_visibility="private")

if __name__ == "__main__":
    demo.queue(default_concurrency_limit=4).launch()
