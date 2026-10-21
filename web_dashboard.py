#!/usr/bin/env python3
"""
web_dashboard.py - Browser front-end for the Perpetual Bistro virtual waiter.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Optional

import db_tools as db
from fsm_agent import STATE_GREETING, STATE_REVIEW, STATE_TAKING_ORDER, FSMAgent, LLMError, OllamaClient

HERE = Path(__file__).resolve().parent
PAGE = HERE / "dashboard.html"
MAX_BODY = 16 * 1024
IMAGES_DIR = HERE / "images"
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg")
IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
               ".webp": "image/webp", ".gif": "image/gif", ".svg": "image/svg+xml"}
IMAGE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*\.(jpg|jpeg|png|webp|gif|svg)$", re.I)
log = logging.getLogger("dashboard")

CATEGORY_EMOJI = {"Mains": "\U0001F37D\uFE0F", "Sides": "\U0001F35F", "Drinks": "\U0001F964", "Desserts": "\U0001F368"}
MENU_META: dict[str, dict[str, str]] = {
    "Cheeseburger": {"emoji": "\U0001F354", "description": "Juicy beef patty, melted cheddar, lettuce, tomato and house sauce on a toasted bun."},
    "Veggie Burger": {"emoji": "\U0001F96C", "description": "Crisp garden-vegetable and bean patty with avocado spread and fresh greens."},
    "Spaghetti Bolognese": {"emoji": "\U0001F35D", "description": "Slow-simmered beef ragu over al dente spaghetti, finished with parmesan."},
    "Chicken Adobo Rice": {"emoji": "\U0001F357", "description": "Soy-vinegar braised chicken, garlic and bay leaf over steamed garlic rice."},
    "Pesto Pasta": {"emoji": "\U0001F33F", "description": "Fresh basil pesto, pine nuts and parmesan tossed through penne."},
    "French Fries": {"emoji": "\U0001F35F", "description": "Golden, crispy shoestring fries lightly salted. Perfect for sharing."},
    "Garden Salad": {"emoji": "\U0001F957", "description": "Mixed greens, cherry tomato, cucumber and carrot with a light vinaigrette."},
    "Onion Rings": {"emoji": "\U0001F9C5", "description": "Thick-cut onions in a crunchy golden batter, served with dipping sauce."},
    "Iced Tea": {"emoji": "\U0001F9CA", "description": "Freshly brewed black tea over ice with a squeeze of lemon."},
    "Mango Smoothie": {"emoji": "\U0001F96D", "description": "Ripe Philippine mango blended with milk and ice. Sweet and creamy."},
    "Cola": {"emoji": "\U0001F964", "description": "Ice-cold classic cola, served in a tall glass."},
    "Bottled Water": {"emoji": "\U0001F4A7", "description": "Purified still water, chilled."},
    "Matcha Sea Salt": {"emoji": "\U0001F375", "description": "Creamy iced matcha topped with a hint of sea salt foam."},
    "Leche Flan": {"emoji": "\U0001F36E", "description": "Silky caramel custard, steamed low and slow the traditional way."},
    "Halo-Halo": {"emoji": "\U0001F367", "description": "Shaved ice with sweet beans, fruit, leche flan, ube and evaporated milk."},
}


def menu_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def find_image(name: str) -> Optional[str]:
    slug = menu_slug(name)
    for ext in IMAGE_EXTS:
        if (IMAGES_DIR / f"{slug}{ext}").is_file():
            return f"/images/{slug}{ext}"
    return None


def decorate_menu(result: dict) -> dict:
    if result.get("status") != "ok":
        return result
    for cat, items in result["menu"].items():
        for it in items:
            meta = MENU_META.get(it["name"], {})
            it["emoji"] = meta.get("emoji", CATEGORY_EMOJI.get(cat, "\U0001F37D\uFE0F"))
            it["description"] = meta.get("description", "A Perpetual Bistro favourite.")
            it["image"] = find_image(it["name"])
    return result


def setup_logging(logfile: Path, verbose: bool) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    log.addHandler(console)
    try:
        f = RotatingFileHandler(logfile, maxBytes=500_000, backupCount=2, encoding="utf-8")
        f.setFormatter(fmt)
        log.addHandler(f)
    except OSError as exc:
        log.warning("Could not write log file %s (%s)", logfile, exc)


class TrackedLLM:
    def __init__(self, client: Callable[[list], str]):
        self.client = client
        self.last_error: Optional[str] = None

    def __call__(self, messages: list) -> str:
        try:
            out = self.client(messages)
            self.last_error = None
            return out
        except LLMError as exc:
            self.last_error = str(exc)
            log.warning("LLM call failed: %s", exc)
            raise


class OllamaMonitor:
    def __init__(self, host: str, model: str, ttl: float = 3.0):
        self.host, self.model, self.ttl = host.rstrip("/"), model, ttl
        self._cache: Optional[dict] = None
        self._at = 0.0

    def status(self) -> dict:
        now = time.monotonic()
        if self._cache and now - self._at < self.ttl:
            return self._cache
        info: dict[str, Any] = {"host": self.host, "model": self.model, "online": False,
                                "model_installed": False, "hint": ""}
        try:
            req = urllib.request.Request(f"{self.host}/api/tags")
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=2) as resp:
                names = [m.get("name", "") for m in json.loads(resp.read()).get("models", [])]
            info["online"] = True
            info["model_installed"] = any(n == self.model or n.startswith(self.model + ":") for n in names)
            if not info["model_installed"]:
                info["hint"] = f"Model not installed. Run: ollama pull {self.model}"
        except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError, ValueError):
            info["hint"] = "Ollama is not running. Start it with: ollama serve"
        if info["online"] != (self._cache or {}).get("online"):
            log.info("Ollama status: %s %s", "ONLINE" if info["online"] else "OFFLINE", info["hint"])
        self._cache, self._at = info, now
        return info


class App:
    def __init__(self, agent: FSMAgent, llm: TrackedLLM, monitor: OllamaMonitor, log_path: Path):
        self.agent, self.llm, self.monitor, self.log_path = agent, llm, monitor, log_path
        self.lock = threading.Lock()
        self.started = time.time()

    def health(self, _body=None) -> tuple[int, Any]:
        return 200, {"server": "ok", "uptime_s": int(time.time() - self.started), "ollama": self.monitor.status()}

    def status(self, _body=None) -> tuple[int, Any]:
        st = self.monitor.status()
        ok = st["online"] and st["model_installed"]
        return 200, {"status": "ok" if ok else "offline", "online": st["online"],
                     "model_installed": st["model_installed"], "model": st["model"], "hint": st["hint"]}

    def payment_methods(self, _body=None) -> tuple[int, Any]:
        return 200, {"methods": [{"name": n, **meta} for n, meta in db.PAYMENT_METHODS.items()]}

    def checkout(self, body: dict) -> tuple[int, Any]:
        method = db.find_payment_method(body.get("payment_method"))
        if method is None:
            return 400, {"status": "error", "error": "invalid_payment",
                         "message": "Please choose one of: " + ", ".join(db.PAYMENT_METHODS) + "."}
        with self.lock:
            sid = self.agent.session_id
            if sid is None:
                return 409, {"status": "error", "error": "no_session",
                             "message": "Tell the waiter your table number first (e.g. 'Table 4')."}
            if db.view_current_order(sid).get("is_empty"):
                return 422, {"status": "error", "error": "empty_order", "message": "Your cart is empty."}
            self.agent.payment_method = method
            self.agent.awaiting_payment = False
            if self.agent.state != STATE_REVIEW:
                self.agent.handle("/confirm")          # moves the FSM into review
            reply = self.agent.handle("/confirm")      # confirms with the chosen payment method
            confirmed = self.agent.state == STATE_GREETING
        log.info("checkout via %s -> %s", method, "confirmed" if confirmed else "not confirmed")
        return 200, {"status": "ok" if confirmed else "error", "confirmed": confirmed,
                     "payment_method": method, "reply": reply}

    def greeting(self, _body=None) -> tuple[int, Any]:
        return 200, {"reply": self.agent.start()}

    def menu(self, _body=None) -> tuple[int, Any]:
        return 200, decorate_menu(db.get_menu_by_category("all"))

    def state(self, _body=None) -> tuple[int, Any]:
        sid = self.agent.session_id
        cart = db.view_current_order(sid) if sid else None
        if cart and cart.get("status") != "ok":
            cart = None
        return 200, {"state": self.agent.state, "table_no": self.agent.table_no, "session_id": sid,
                     "cart": cart, "menu": decorate_menu(db.get_menu_by_category("all"))}

    def conversation_log(self, _body=None) -> tuple[int, Any]:
        return 200, self.agent.log

    def chat(self, body: dict) -> tuple[int, Any]:
        text = str(body.get("text", "")).strip()[:500]
        if not text:
            return 400, {"error": "empty_message", "message": "Please type something."}
        log.info("customer: %s", text)
        with self.lock:
            self.llm.last_error = None
            reply = self.agent.handle(text)
            degraded = self.llm.last_error is not None
        log.info("waiter (%s%s): %s", self.agent.state, ", DEGRADED" if degraded else "", reply.splitlines()[0][:100])
        needs_payment = self.agent.awaiting_payment and self.agent.state == STATE_REVIEW
        return 200, {"reply": reply, "degraded": degraded, "needs_payment": needs_payment}

    def order_add(self, body: dict) -> tuple[int, Any]:
        name = str(body.get("item_name", "")).strip()
        if not name:
            return 400, {"status": "error", "error": "missing_item", "message": "item_name is required."}
        with self.lock:
            sid = self.agent.session_id
            if sid is None:
                return 409, {"status": "error", "error": "no_session",
                             "message": "Tell the waiter your table number first (e.g. 'Table 4')."}
            res = db.add_item_to_order(sid, name, body.get("qty", 1), body.get("instructions", ""))
            if res["status"] == "ok":
                self._sync_agent(f"added {res['added']['quantity']}x {res['added']['item']}")
            cart = db.view_current_order(sid)
            res["cart"] = cart
        log.info("direct add %r -> %s", name, res.get("error", "ok"))
        return (200 if res["status"] == "ok" else 422), res

    def order_add_many(self, body: dict) -> tuple[int, Any]:
        items = body.get("items")
        if not isinstance(items, list) or not items or len(items) > 30:
            return 400, {"status": "error", "error": "bad_items", "message": "items must be a list of 1-30 dishes."}
        results: list[dict] = []
        with self.lock:
            sid = self.agent.session_id
            if sid is None:
                return 409, {"status": "error", "error": "no_session",
                             "message": "Tell the waiter your table number first (e.g. 'Table 4')."}
            for it in items:
                name = str(it.get("item_name", "")).strip() if isinstance(it, dict) else ""
                if not name:
                    results.append({"item_name": name, "status": "error", "message": "Missing item_name."})
                    continue
                res = db.add_item_to_order(sid, name, it.get("qty", 1), it.get("instructions", ""))
                results.append({"item_name": name, "status": res["status"], "error": res.get("error"),
                                "message": res.get("message"), "added": res.get("added")})
            done = [r for r in results if r["status"] == "ok"]
            if done:
                self._sync_agent("added " + ", ".join(f"{r['added']['quantity']}x {r['added']['item']}" for r in done))
            cart = db.view_current_order(sid)
        status = "ok" if len(done) == len(results) else ("partial" if done else "error")
        log.info("direct add-many: %d/%d added", len(done), len(results))
        return (200 if done else 422), {"status": status, "results": results, "cart": cart}

    def order_remove(self, body: dict) -> tuple[int, Any]:
        name = str(body.get("item_name", "")).strip()
        if not name:
            return 400, {"status": "error", "error": "missing_item", "message": "item_name is required."}
        with self.lock:
            sid = self.agent.session_id
            if sid is None:
                return 409, {"status": "error", "error": "no_session", "message": "No open order."}
            res = db.remove_item_from_order(sid, name, body.get("qty"))
            if res["status"] == "ok":
                self._sync_agent(f"removed {res['removed']['quantity']}x {res['removed']['item']}")
            cart = db.view_current_order(sid)
            res["cart"] = cart
        log.info("direct remove %r -> %s", name, res.get("error", "ok"))
        return (200 if res["status"] == "ok" else 422), res

    def reset(self, _body=None) -> tuple[int, Any]:
        with self.lock:
            self.agent.shutdown()
            self.agent.state = STATE_GREETING
            self.agent.table_no = None
            self.agent.messages = []
            self.agent.payment_method = None
            self.agent.awaiting_payment = False
        log.info("session reset")
        return 200, {"reply": self.agent.start()}

    def _sync_agent(self, what: str) -> None:
        if self.agent.state == STATE_REVIEW:
            self.agent.state = STATE_TAKING_ORDER
        self.agent.awaiting_payment = False
        if self.agent.messages:
            self.agent.messages.append({"role": "user", "content": f"SYSTEM: the customer {what} using the on-screen menu."})


class DashboardServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True


def make_handler(app: App, cors: bool):
    routes: dict[tuple[str, str], Callable[[Any], tuple[int, Any]]] = {
        ("GET", "/api/health"): app.health,
        ("GET", "/api/status"): app.status,
        ("GET", "/api/payment-methods"): app.payment_methods,
        ("POST", "/api/checkout"): app.checkout,
        ("GET", "/api/greeting"): app.greeting,
        ("GET", "/api/state"): app.state,
        ("GET", "/api/menu"): app.menu,
        ("GET", "/api/log"): app.conversation_log,
        ("POST", "/api/chat"): app.chat,
        ("POST", "/api/order/add"): app.order_add,
        ("POST", "/api/order/add-many"): app.order_add_many,
        ("POST", "/api/order/remove"): app.order_remove,
        ("POST", "/api/reset"): app.reset,
    }
    known_paths = {p for _, p in routes} | {"/", "/index.html", "/favicon.ico"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "BistroDashboard/2.1"

        def log_message(self, fmt, *args):
            pass

        def _send(self, code: int, body: bytes, ctype: str, extra: Optional[dict] = None, cache: str = "no-store") -> None:
            self.send_response(code)
            textual = ctype.startswith("text/") or ctype.endswith("json")
            self.send_header("Content-Type", f"{ctype}; charset=utf-8" if textual else ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            if cors:
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj: Any, extra: Optional[dict] = None) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json", extra)

        def _read_json(self) -> dict:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise Exception("Bad Content-Length header.")
            if length > MAX_BODY:
                raise Exception("Request too large.")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                raise Exception("Body is not valid JSON.")
            if not isinstance(data, dict):
                raise Exception("JSON body must be an object.")
            return data

        def _handle(self, method: str) -> None:
            t0 = time.perf_counter()
            path = self.path.split("?", 1)[0]
            code = 500
            try:
                if method == "GET" and path in ("/", "/index.html"):
                    if not PAGE.exists():
                        raise FileNotFoundError(f"dashboard.html not found next to web_dashboard.py ({PAGE}).")
                    code = 200
                    self._send(200, PAGE.read_bytes(), "text/html")
                elif method == "GET" and path.startswith("/images/"):
                    name = path[len("/images/"):]
                    file = IMAGES_DIR / name
                    if not IMAGE_NAME_RE.match(name) or not file.is_file():
                        code = 404
                        self._json(404, {"error": "http_error", "message": "Image not found."})
                        return
                    code = 200
                    self._send(200, file.read_bytes(), IMAGE_TYPES[file.suffix.lower()], cache="public, max-age=300")
                elif path == "/favicon.ico":
                    code = 204
                    self.send_response(204)
                    self.end_headers()
                elif (method, path) in routes:
                    body = self._read_json() if method == "POST" else None
                    code, payload = routes[(method, path)](body)
                    extra = {"Content-Disposition": 'attachment; filename="conversation_log.json"'} \
                        if path == "/api/log" and "download" in self.path else None
                    self._json(code, payload, extra)
                elif path in known_paths:
                    code = 405
                    self._json(405, {"error": "http_error", "message": f"{method} not allowed on {path}."})
                else:
                    code = 404
                    self._json(404, {"error": "http_error", "message": f"No such page: {path}"})
            except Exception as exc:
                code = 500
                log.exception("Unhandled error on %s %s", method, path)
                try:
                    self._json(500, {"error": "internal_error", "message": str(exc)})
                except OSError:
                    pass
            finally:
                if path not in ("/api/health", "/api/state", "/api/status"):
                    log.info("%s %s -> %s (%d ms)", method, path, code, (time.perf_counter() - t0) * 1000)

        def do_GET(self): self._handle("GET")
        def do_POST(self): self._handle("POST")
        def do_OPTIONS(self):
            if cors: self._send(204, b"", "text/plain")
            else: self._json(405, {"error": "http_error", "message": "CORS disabled."})

    return Handler


def bind_server(bind: str, port: int, handler, tries: int = 10) -> DashboardServer:
    for p in range(port, port + tries):
        try:
            return DashboardServer((bind, p), handler)
        except OSError as exc:
            log.warning("Port %d not available (%s) - trying %d", p, exc.strerror or exc, p + 1)
    sys.exit(f"No free port between {port} and {port + tries - 1}.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Perpetual Bistro web dashboard")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--model", default="qwen2.5:1.5b")
    ap.add_argument("--ollama-host", default="http://localhost:11434")
    ap.add_argument("--db", default=str(HERE / "restaurant.db"))
    ap.add_argument("--log", default=str(HERE / "conversation_log.json"))
    ap.add_argument("--logfile", default=str(HERE / "dashboard.log"))
    ap.add_argument("--reset-db", action="store_true")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--cors", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    setup_logging(Path(args.logfile), args.verbose)
    db.set_db_path(args.db)
    db.init_db(reset=args.reset_db)
    try:
        IMAGES_DIR.mkdir(exist_ok=True)
    except OSError:
        pass

    monitor = OllamaMonitor(args.ollama_host, args.model)
    st = monitor.status()
    if not st["online"] or not st["model_installed"]:
        log.warning("AI is unavailable: %s", st["hint"])

    llm = TrackedLLM(OllamaClient(args.model, args.ollama_host))
    agent = FSMAgent(llm, llm_label=args.model)
    app = App(agent, llm, monitor, Path(args.log))

    server = bind_server(args.bind, args.port, make_handler(app, args.cors))
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}"
    log.info("Perpetual Bistro dashboard running at %s (Ctrl+C to stop)", url)

    if not args.no_browser:
        threading.Timer(0.8, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        agent.shutdown()
        agent.save_log(args.log)


if __name__ == "__main__":
    main()