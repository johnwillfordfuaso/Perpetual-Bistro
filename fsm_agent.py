"""
fsm_agent.py - Finite State Machine controller for the Perpetual Bistro virtual waiter.
"""
from __future__ import annotations

import inspect
import json
import re
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Callable, Optional

import db_tools as db

STATE_GREETING = "STATE_GREETING"
STATE_TAKING_ORDER = "STATE_TAKING_ORDER"
STATE_REVIEW = "STATE_REVIEW"
STATE_CONFIRMED = "STATE_CONFIRMED"

MAX_TOOL_ROUNDS = 5
MAX_CALLS_PER_ROUND = 4
MAX_HISTORY = 60

WELCOME = "Welcome to Perpetual Bistro! Which table number are you seated at?"


class LLMError(RuntimeError):
    """Raised when the language model cannot be reached or answers badly."""


class OllamaClient:
    def __init__(self, model: str = "qwen2.5:1.5b", host: str = "http://localhost:11434",
                 temperature: float = 0.1, timeout: int = 45):
        self.model, self.host, self.temperature, self.timeout = model, host.rstrip("/"), temperature, timeout

    def __call__(self, messages: list[dict]) -> str:
        payload = json.dumps({
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": self.temperature,
                "num_predict": 128,
                "num_ctx": 2048,
            },
        }).encode()
        req = urllib.request.Request(f"{self.host}/api/chat", data=payload,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read())
            return data["message"]["content"]
        except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError) as exc:
            raise LLMError(f"Ollama request failed: {exc}") from exc


def parse_tool_calls(raw: str) -> list[dict]:
    calls, decoder, i = [], json.JSONDecoder(), 0
    while True:
        i = raw.find("{", i)
        if i == -1:
            break
        try:
            obj, end = decoder.raw_decode(raw, i)
        except json.JSONDecodeError:
            i += 1
            continue
        name = obj.get("tool") or obj.get("name") if isinstance(obj, dict) else None
        if isinstance(name, str):
            args = obj.get("arguments", obj.get("parameters", {}))
            calls.append({"tool": name, "arguments": args if isinstance(args, dict) else {}})
            i = end
        else:
            i += 1
    return calls[:MAX_CALLS_PER_ROUND]


def money(x: float) -> str:
    return f"{x:,.2f}"


def format_tab(view: dict) -> str:
    if view.get("is_empty"):
        return "(Your tab is empty.)"
    rows = []
    for l in view["items"]:
        label = l["name"] + (f" ({l['instructions']})" if l["instructions"] else "")
        rows.append(f"  {l['quantity']}x {label:<46} {money(l['line_total']):>9}")
    rule = "  " + "-" * 60
    return "\n".join(
        rows + [
            rule,
            f"  {'Subtotal:':<50} {money(view['subtotal']):>9}",
            f"  {'Service fee (' + str(int(view['service_fee_rate'] * 100)) + '%):':<50} {money(view['service_fee']):>9}",
            f"  {'TOTAL (' + view['currency'] + '):':<50} {money(view['grand_total']):>9}",
        ]
    )


def format_menu(result: dict) -> str:
    out = []
    for cat, items in result["menu"].items():
        out.append(cat.upper())
        for it in items:
            tags = []
            if it["vegetarian"]:
                tags.append("veg")
            if not it["available"]:
                tags.append("SOLD OUT")
            tag = f"  [{', '.join(tags)}]" if tags else ""
            out.append(f"  {it['name']:<22} {money(it['price']):>8}{tag}")
    return "\n".join(out)


def format_receipt(r: dict) -> str:
    return "\n".join([
        "=" * 64,
        "  PERPETUAL BISTRO - OFFICIAL RECEIPT",
        f"  Table {r['table_no']}   Session #{r['session_id']}   {r['confirmed_at']}",
        "=" * 64,
        format_tab({**r, "is_empty": False, "service_fee_rate": db.SERVICE_FEE_RATE}),
        "-" * 64,
        f"  Payment method: {r.get('payment_method', '-')}",
        f"  {r.get('payment_note', '')}",
        "=" * 64,
        "  Your order has been sent to the kitchen. Thank you!",
        "=" * 64,
    ])


_WORD_NUMBERS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve".split())}
_STOP = set("hi hello hey i im i'm we're were at on in the a an is it's its sitting seated table number no "
            "please thanks thank you good morning afternoon evening our my".split())

DONE_RE = re.compile(
    r"\b(that'?s (all|it)|i'?m (done|finished)|we'?re (done|finished)|that will be (all|it)|"
    r"(the |my )?(bill|check)\b|check ?out|ready to (pay|confirm)|nothing (more|else))", re.I)
YES_RE = re.compile(
    r"^\s*(yes|yeah|yep|yup|ok(ay)?|sure|confirm(ed)?|correct|proceed|go ahead|looks good|"
    r"that'?s (right|correct)|send( it)?( to (the )?kitchen)?)\s*[.!]*\s*$", re.I)
NO_RE = re.compile(r"^\s*(no|nope|nah|wait|not yet)\s*[.!]*\s*$", re.I)
CLEAR_RE = re.compile(
    r"^\s*(cancel( (the|my))? (whole |entire )?order|cancel everything|start over|"
    r"clear (the |my )?(cart|order|tab))\b", re.I)


def extract_table_number(text: str) -> tuple[Optional[int], bool]:
    low = text.lower()
    m = re.search(r"\btable\s*(?:no\.?|number|#)?\s*(\d{1,2}|[a-z]+)\b", low)
    token = m.group(1) if m else None
    if token is None:
        words = re.findall(r"[\w']+", low)
        if len(words) <= 4:
            token = next((w for w in words if w.isdigit() or w in _WORD_NUMBERS), None)
    if token is None:
        return None, False
    number = int(token) if token.isdigit() else _WORD_NUMBERS.get(token)
    if number is None:
        return None, False
    leftover = [w for w in re.findall(r"[a-z']+", low) if w not in _STOP and w not in _WORD_NUMBERS]
    return number, len(leftover) >= 3


class FSMAgent:
    def __init__(self, llm_chat: Callable[[list[dict]], str], llm_label: str = "qwen2.5:1.5b"):
        self.llm_chat = llm_chat
        self.llm_label = llm_label
        self.state = STATE_GREETING
        self.session_id: Optional[int] = None
        self.table_no: Optional[int] = None
        self.messages: list[dict] = []
        self.log: dict[str, Any] = {
            "meta": {"llm": llm_label, "started_at": datetime.now().isoformat(timespec="seconds"),
                     "service_fee_rate": db.SERVICE_FEE_RATE, "currency": db.CURRENCY},
            "turns": [],
        }
        self._turn: dict[str, Any] = {}
        self._review_requested = False
        self.payment_method: Optional[str] = None
        self.awaiting_payment = False

    def start(self) -> str:
        return WELCOME

    def handle(self, text: str) -> str:
        text = (text or "").strip()
        if not text:
            return "I'm here whenever you're ready."
        before = self.state
        self._turn = {"tool_calls": [], "transitions": []}
        try:
            if text.startswith("/"):
                reply = self._command(text)
            elif self.state == STATE_GREETING:
                reply = self._greeting(text)
            elif self.state == STATE_TAKING_ORDER:
                reply = self._taking_order(text)
            else:
                reply = self._review(text)
        except LLMError:
            reply = ("Sorry, my ordering assistant is not responding right now. "
                     "Your tab is safe - please try again in a moment, or use /cart and /confirm.")
        except Exception:
            reply = "Sorry, something went wrong on my side. Your tab is unchanged - please try again."
        self._record(text, before, reply)
        return reply

    def save_log(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.log, fh, indent=2, ensure_ascii=False)

    def shutdown(self) -> None:
        if self.session_id is not None:
            db.cancel_session(self.session_id)
            self.session_id = None

    def _set_state(self, new: str, reason: str) -> None:
        self._turn["transitions"].append({"from": self.state, "to": new, "reason": reason})
        self.state = new

    def _record(self, user_text: str, before: str, reply: str) -> None:
        self.log["turns"].append({
            "turn": len(self.log["turns"]) + 1,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "user": user_text,
            "state_before": before,
            "tool_calls": self._turn["tool_calls"],
            "state_transitions": self._turn["transitions"],
            "state_after": self.state,
            "assistant": reply,
            "db_state": db.snapshot(self.session_id if self.session_id else self._last_session()),
        })

    def _last_session(self) -> Optional[int]:
        for t in reversed(self.log["turns"]):
            sid = t["db_state"].get("session", {}).get("session_id")
            if sid:
                return sid
        return None

    def _system_prompt(self) -> str:
        specs = json.dumps(db.TOOL_SPECS, indent=1)
        return f"""You are the virtual waiter at Perpetual Bistro, serving Table {self.table_no}.
Prices are in {db.CURRENCY}. Be concise (1-2 sentences).

CRITICAL INSTRUCTIONS:
To execute actions (like adding items to the order), you MUST output ONLY a valid JSON object matching this format:
{{"tool": "<tool_name>", "arguments": {{...}}}}
Do NOT write "TOOL_RESULT" in your text response. Only output JSON when executing a tool call.

AVAILABLE TOOLS:
{specs}"""

    def _new_session(self, table_no: int) -> dict:
        res = db.create_table_session(table_no)
        if res["status"] == "ok":
            self.session_id, self.table_no = res["session_id"], res["table_no"]
            self.payment_method, self.awaiting_payment = None, False
            self.messages = [{"role": "system", "content": self._system_prompt()}]
        return res

    def _reset_for_next_customer(self) -> None:
        self.session_id = self.table_no = None
        self.messages = []
        self.payment_method, self.awaiting_payment = None, False
        self._set_state(STATE_GREETING, "session reset for next customer")

    def _greeting(self, text: str) -> str:
        table, extra = extract_table_number(text)
        if table is None:
            return "Sorry, I didn't catch that. Which table number are you seated at (for example, 'Table 4')?"
        res = self._new_session(table)
        if res["status"] != "ok":
            return f"{res['message']} Could you tell me your table number again?"
        self._set_state(STATE_TAKING_ORDER, f"table {table} captured, session {self.session_id} opened")
        if extra:
            return self._taking_order(text)
        return (f"Lovely, Table {table} is all set! What can I get for you? "
                "You can ask about the menu (type /menu to see everything).")

    def _taking_order(self, text: str) -> str:
        if CLEAR_RE.match(text):
            return self._clear()
        if len(text.split()) <= 8 and DONE_RE.search(text):
            view = db.view_current_order(self.session_id)
            if view["is_empty"]:
                return "Your tab is empty so far - what would you like to order?"
            return self._enter_review("customer finished ordering")
        self.messages.append({"role": "user", "content": text})
        return self._run_llm()

    def _run_llm(self) -> str:
        mark = len(self.messages) - 1
        retried = False
        try:
            for _ in range(MAX_TOOL_ROUNDS):
                raw = self.llm_chat(self.messages).strip()
                calls = parse_tool_calls(raw)

                if not calls:
                    if '"tool"' in raw and not retried:
                        retried = True
                        self.messages += [
                            {"role": "assistant", "content": raw},
                            {"role": "user", "content": "SYSTEM: invalid JSON tool call format. Reply with valid JSON only."},
                        ]
                        continue
                    self.messages.append({"role": "assistant", "content": raw})
                    self._trim_history()
                    return raw or "Sorry, could you say that again?"

                self.messages.append({"role": "assistant", "content": raw})
                results = []
                for call in calls:
                    result = self._execute_tool(call)
                    self._turn["tool_calls"].append({**call, "result": result})
                    results.append({"tool": call["tool"], "result": result})
                if self._review_requested:
                    self._review_requested = False
                    self.messages.append({"role": "user", "content": "TOOL_RESULT: review started"})
                    return self._enter_review("LLM called request_review")
                self.messages.append({"role": "user", "content": "TOOL_RESULT: " + json.dumps(results)})
        except LLMError:
            del self.messages[mark:]
            raise
        return "Sorry, I had trouble processing that. Could you rephrase your request?"

    def _trim_history(self) -> None:
        if len(self.messages) > MAX_HISTORY:
            self.messages = self.messages[:1] + self.messages[-(MAX_HISTORY - 1):]

    def _execute_tool(self, call: dict) -> dict:
        name, args = call["tool"], dict(call["arguments"])
        if name == "request_review":
            view = db.view_current_order(self.session_id)
            if view["is_empty"]:
                return db._err("empty_order", "The tab is empty; nothing to review yet.")
            self._review_requested = True
            return {"status": "ok", "message": "Moving to review."}
        fn = db.LLM_TOOLS.get(name)
        if fn is None:
            return db._err("unknown_tool", f"There is no tool named {name!r}.")
        params = inspect.signature(fn).parameters
        args = {k: v for k, v in args.items() if k in params and k != "session_id"}
        if "session_id" in params:
            args["session_id"] = self.session_id
        try:
            inspect.signature(fn).bind(**args)
        except TypeError as exc:
            return db._err("invalid_arguments", f"Bad arguments for {name}: {exc}")
        try:
            return fn(**args)
        except Exception as exc:
            return db._err("tool_failure", f"{name} failed unexpectedly ({type(exc).__name__}).")

    def _clear(self) -> str:
        db.clear_order(self.session_id)
        if self.state != STATE_TAKING_ORDER:
            self._set_state(STATE_TAKING_ORDER, "order cleared")
        self.messages.append({"role": "user", "content": "SYSTEM: the customer cleared their whole order."})
        return "No problem - I've cleared your tab. What would you like instead?"

    def _enter_review(self, reason: str) -> str:
        self._set_state(STATE_REVIEW, reason)
        view = db.view_current_order(self.session_id)
        self.messages.append({"role": "assistant", "content": "(showed itemised tab)"})
        return (f"Here is your order for Table {self.table_no}:\n\n{format_tab(view)}\n\n"
                "Shall I send this to the kitchen? Say 'yes' to confirm and tell me how you'd like to pay "
                f"({', '.join(db.PAYMENT_METHODS)}), or tell me what to change.")

    def _review(self, text: str) -> str:
        method = db.find_payment_method(text) if len(text.split()) <= 8 else None
        if method:
            self.payment_method = method
            return self._confirm()
        if YES_RE.match(text):
            return self._confirm()
        if CLEAR_RE.match(text):
            return self._clear()
        self.awaiting_payment = False
        self._set_state(STATE_TAKING_ORDER, "customer wants changes")
        if NO_RE.match(text):
            return "Of course - what would you like to change?"
        return self._taking_order(text)

    def _confirm(self) -> str:
        view = db.view_current_order(self.session_id)
        if view["is_empty"]:
            self._set_state(STATE_TAKING_ORDER, "nothing to confirm")
            return "Your tab is empty - what would you like to order?"
        if not self.payment_method:
            self.awaiting_payment = True
            return ("Almost done! How would you like to pay? We accept "
                    + ", ".join(db.PAYMENT_METHODS) + ".")
        self.awaiting_payment = False
        self._set_state(STATE_CONFIRMED, "customer confirmed order")
        result = db.confirm_order(self.session_id, self.payment_method)
        self._turn["tool_calls"].append({"tool": "confirm_order",
                                         "arguments": {"session_id": self.session_id, "payment_method": self.payment_method},
                                         "result": result})
        if result["status"] != "ok":
            self._set_state(STATE_TAKING_ORDER, f"commit failed: {result['error']}")
            problems = "; ".join(f"{p['item']} (no longer available)" for p in result.get("problems", [])) or result["message"]
            return f"I'm sorry, I couldn't confirm that: {problems}. Please adjust your order."
        receipt = format_receipt(result)
        self._reset_for_next_customer()
        return f"{receipt}\n\n{WELCOME}"

    def _command(self, text: str) -> str:
        cmd = text.split()[0].lower()
        if cmd == "/help":
            return "Commands: /menu /cart /confirm /clear /state /quit"
        if cmd == "/menu":
            return format_menu(db.get_menu_by_category("all"))
        if cmd == "/state":
            return f"State: {self.state} | table: {self.table_no} | session: {self.session_id}"
        if self.session_id is None:
            return "Please tell me your table number first."
        if cmd == "/cart":
            return format_tab(db.view_current_order(self.session_id))
        if cmd == "/clear":
            return self._clear()
        if cmd == "/confirm":
            if self.state == STATE_REVIEW:
                return self._confirm()
            view = db.view_current_order(self.session_id)
            if view["is_empty"]:
                return "Your tab is empty - what would you like to order?"
            return self._enter_review("/confirm command")
        return "Unknown command. Type /help to see options."