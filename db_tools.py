"""
db_tools.py - Database operations and tool definitions for Perpetual Bistro.
"""
from __future__ import annotations
import re
import sqlite3
from pathlib import Path
from typing import Any, Optional

CURRENCY = "PHP"
SERVICE_FEE_RATE = 0.10

# Payment methods offered at checkout (name -> details shown in the UI)
PAYMENT_METHODS = {
    "GCash":     {"type": "online", "icon": "\U0001F4F1", "label": "E-wallet",
                  "note": "Pay with GCash. Show your payment confirmation to the cashier."},
    "Cash":      {"type": "cash",   "icon": "\U0001F4B5", "label": "At the table",
                  "note": "Please hand your cash to the server or cashier."},
    "BPI":       {"type": "online", "icon": "\U0001F3E6", "label": "Online banking",
                  "note": "Pay with BPI. Show your payment confirmation to the cashier."},
    "Metrobank": {"type": "online", "icon": "\U0001F3E6", "label": "Online banking",
                  "note": "Pay with Metrobank. Show your payment confirmation to the cashier."},
    "Maribank":  {"type": "online", "icon": "\U0001F3E6", "label": "Digital bank",
                  "note": "Pay with Maribank. Show your payment confirmation to the cashier."},
}
_PAYMENT_ALIASES = {
    "gcash": "GCash", "g-cash": "GCash", "g cash": "GCash",
    "cash": "Cash",
    "bpi": "BPI", "bank of the philippine islands": "BPI",
    "metrobank": "Metrobank", "metro bank": "Metrobank",
    "maribank": "Maribank", "mari bank": "Maribank",
}


def find_payment_method(text) -> Optional[str]:
    """Return the canonical payment method named in `text`, or None."""
    low = str(text or "").lower().strip()
    if not low:
        return None
    if low in _PAYMENT_ALIASES:
        return _PAYMENT_ALIASES[low]
    for alias in sorted(_PAYMENT_ALIASES, key=len, reverse=True):
        if re.search(r"(?<![a-z])" + re.escape(alias) + r"(?![a-z])", low):
            return _PAYMENT_ALIASES[alias]
    return None

_DB_PATH = Path(__file__).parent / "restaurant.db"

def set_db_path(path: str | Path) -> None:
    global _DB_PATH
    _DB_PATH = Path(path)

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db(reset: bool = False) -> None:
    if reset and _DB_PATH.exists():
        _DB_PATH.unlink()
    conn = _get_conn()
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS menu (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        category TEXT NOT NULL,
        price REAL NOT NULL,
        available INTEGER NOT NULL DEFAULT 1,
        description TEXT,
        vegetarian INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS sessions (
        session_id INTEGER PRIMARY KEY AUTOINCREMENT,
        table_no INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        payment_method TEXT
    );

    CREATE TABLE IF NOT EXISTS order_items (
        item_id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        quantity INTEGER NOT NULL,
        instructions TEXT,
        unit_price REAL NOT NULL,
        FOREIGN KEY (session_id) REFERENCES sessions(session_id),
        FOREIGN KEY (name) REFERENCES menu(name)
    );
    """)
    
    # Migration: databases created before payments existed lack this column
    cols = [r[1] for r in cur.execute("PRAGMA table_info(sessions)").fetchall()]
    if "payment_method" not in cols:
        cur.execute("ALTER TABLE sessions ADD COLUMN payment_method TEXT")

    # Seed menu if empty
    cur.execute("SELECT COUNT(*) FROM menu")
    if cur.fetchone()[0] == 0:
        default_menu = [
            ("Cheeseburger", "Mains", 180.00, 1, "Juicy beef patty, melted cheddar, lettuce, tomato and house sauce on a toasted bun.", 0),
            ("Veggie Burger", "Mains", 170.00, 1, "Crisp garden-vegetable and bean patty with avocado spread and fresh greens.", 1),
            ("Spaghetti Bolognese", "Mains", 220.00, 1, "Slow-simmered beef ragu over al dente spaghetti, finished with parmesan.", 0),
            ("Chicken Adobo Rice", "Mains", 160.00, 1, "Soy-vinegar braised chicken, garlic and bay leaf over steamed garlic rice.", 0),
            ("Pesto Pasta", "Mains", 200.00, 1, "Fresh basil pesto, pine nuts and parmesan tossed through penne.", 1),
            ("French Fries", "Sides", 80.00, 1, "Golden, crispy shoestring fries lightly salted. Perfect for sharing.", 1),
            ("Garden Salad", "Sides", 95.00, 1, "Mixed greens, cherry tomato, cucumber and carrot with a light vinaigrette.", 1),
            ("Onion Rings", "Sides", 90.00, 0, "Thick-cut onions in a crunchy golden batter, served with dipping sauce.", 1),
            ("Iced Tea", "Drinks", 75.00, 1, "Freshly brewed black tea over ice with a squeeze of lemon.", 1),
            ("Mango Smoothie", "Drinks", 110.00, 1, "Ripe Philippine mango blended with milk and ice. Sweet and creamy.", 1),
            ("Cola", "Drinks", 65.00, 1, "Ice-cold classic cola, served in a tall glass.", 1),
            ("Bottled Water", "Drinks", 40.00, 1, "Purified still water, chilled.", 1),
            ("Matcha Sea Salt", "Drinks", 120.00, 1, "Creamy iced matcha topped with a hint of sea salt foam.", 1),
            ("Leche Flan", "Desserts", 90.00, 1, "Silky caramel custard, steamed low and slow the traditional way.", 1),
            ("Halo-Halo", "Desserts", 130.00, 1, "Shaved ice with sweet beans, fruit, leche flan, ube and evaporated milk.", 1),
        ]
        cur.executemany("""
            INSERT OR IGNORE INTO menu (name, category, price, available, description, vegetarian)
            VALUES (?, ?, ?, ?, ?, ?)
        """, default_menu)
    conn.commit()
    conn.close()

def _err(code: str, message: str) -> dict:
    return {"status": "error", "error": code, "message": message}

def create_table_session(table_no: int) -> dict:
    if not isinstance(table_no, int) or not (1 <= table_no <= 50):
        return _err("invalid_table", "Table number must be between 1 and 50.")
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO sessions (table_no, status) VALUES (?, 'active')", (table_no,))
    session_id = cur.lastrowid
    conn.commit()
    conn.close()
    return {"status": "ok", "session_id": session_id, "table_no": table_no}

def add_item_to_order(session_id: int, item_name: str, qty: int = 1, instructions: str = "") -> dict:
    if not isinstance(qty, int) or qty <= 0:
        return _err("invalid_qty", "Quantity must be a positive integer.")
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT price, available FROM menu WHERE name COLLATE NOCASE = ?", (item_name.strip(),))
    row = cur.fetchone()
    if not row:
        conn.close()
        return _err("unknown_item", f"Item {item_name!r} is not on the menu.")
    if not row["available"]:
        conn.close()
        return _err("sold_out", f"Item {item_name!r} is currently sold out.")
    price = row["price"]
    
    cur.execute("SELECT name FROM menu WHERE name COLLATE NOCASE = ?", (item_name.strip(),))
    canonical_name = cur.fetchone()["name"]

    cur.execute("""
        INSERT INTO order_items (session_id, name, quantity, instructions, unit_price)
        VALUES (?, ?, ?, ?, ?)
    """, (session_id, canonical_name, qty, instructions.strip(), price))
    conn.commit()
    conn.close()
    return {"status": "ok", "added": {"item": canonical_name, "quantity": qty, "instructions": instructions.strip(), "unit_price": price}}

def remove_item_from_order(session_id: int, item_name: str, qty: Optional[int] = None) -> dict:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT name FROM menu WHERE name COLLATE NOCASE = ?", (item_name.strip(),))
    row = cur.fetchone()
    if not row:
        conn.close()
        return _err("unknown_item", f"Item {item_name!r} is not on the menu.")
    canonical_name = row["name"]

    cur.execute("SELECT item_id, quantity FROM order_items WHERE session_id = ? AND name = ?", (session_id, canonical_name))
    items = cur.fetchall()
    if not items:
        conn.close()
        return _err("not_in_order", f"Item {canonical_name!r} is not in your current order.")
    
    if qty is None:
        cur.execute("DELETE FROM order_items WHERE session_id = ? AND name = ?", (session_id, canonical_name))
        removed_qty = sum(i["quantity"] for i in items)
    else:
        remaining = qty
        removed_qty = 0
        for item in items:
            if remaining <= 0:
                break
            if item["quantity"] <= remaining:
                cur.execute("DELETE FROM order_items WHERE item_id = ?", (item["item_id"],))
                remaining -= item["quantity"]
                removed_qty += item["quantity"]
            else:
                new_qty = item["quantity"] - remaining
                cur.execute("UPDATE order_items SET quantity = ? WHERE item_id = ?", (new_qty, item["item_id"]))
                removed_qty += remaining
                remaining = 0
    conn.commit()
    conn.close()
    return {"status": "ok", "removed": {"item": canonical_name, "quantity": removed_qty}}

def view_current_order(session_id: int) -> dict:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT table_no, status FROM sessions WHERE session_id = ?", (session_id,))
    session = cur.fetchone()
    if not session:
        conn.close()
        return _err("invalid_session", "Session not found.")
    
    cur.execute("""
        SELECT name, SUM(quantity) as quantity, instructions, unit_price
        FROM order_items WHERE session_id = ?
        GROUP BY name, instructions
    """, (session_id,))
    rows = cur.fetchall()
    conn.close()

    items = []
    subtotal = 0.0
    for r in rows:
        line_total = r["quantity"] * r["unit_price"]
        subtotal += line_total
        items.append({
            "name": r["name"],
            "quantity": r["quantity"],
            "instructions": r["instructions"] or "",
            "unit_price": r["unit_price"],
            "line_total": line_total
        })
    
    service_fee = subtotal * SERVICE_FEE_RATE
    grand_total = subtotal + service_fee

    return {
        "status": "ok",
        "session_id": session_id,
        "table_no": session["table_no"],
        "is_empty": len(items) == 0,
        "items": items,
        "subtotal": subtotal,
        "service_fee_rate": SERVICE_FEE_RATE,
        "service_fee": service_fee,
        "grand_total": grand_total,
        "currency": CURRENCY
    }

def clear_order(session_id: int) -> dict:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM order_items WHERE session_id = ?", (session_id,))
    conn.commit()
    conn.close()
    return {"status": "ok", "message": "Order cleared."}

def confirm_order(session_id: int, payment_method: Optional[str] = None) -> dict:
    view = view_current_order(session_id)
    if view.get("status") != "ok":
        return view
    if view["is_empty"]:
        return _err("empty_order", "Cannot confirm an empty order.")
    method = find_payment_method(payment_method)
    if method is None:
        return _err("invalid_payment", "Please choose a payment method: " + ", ".join(PAYMENT_METHODS) + ".")
    
    conn = _get_conn()
    cur = conn.cursor()
    problems = []
    for item in view["items"]:
        cur.execute("SELECT available FROM menu WHERE name = ?", (item["name"],))
        row = cur.fetchone()
        if not row or not row["available"]:
            problems.append({"item": item["name"], "available": 0})
    
    if problems:
        conn.close()
        return {"status": "error", "error": "item_unavailable", "message": "Some items are no longer available.", "problems": problems}
    
    cur.execute("UPDATE sessions SET status = 'confirmed', payment_method = ? WHERE session_id = ?",
                (method, session_id))
    conn.commit()
    conn.close()
    
    return {
        "payment_method": method,
        "payment_type": PAYMENT_METHODS[method]["type"],
        "payment_note": PAYMENT_METHODS[method]["note"],
        "status": "ok",
        "session_id": session_id,
        "table_no": view["table_no"],
        "items": view["items"],
        "subtotal": view["subtotal"],
        "service_fee": view["service_fee"],
        "grand_total": view["grand_total"],
        "currency": CURRENCY,
        "confirmed_at": "Just now"
    }

def cancel_session(session_id: int) -> dict:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE sessions SET status = 'cancelled' WHERE session_id = ?", (session_id,))
    conn.commit()
    conn.close()
    return {"status": "ok", "message": "Session cancelled."}

def get_menu_by_category(category: str = "all") -> dict:
    conn = _get_conn()
    cur = conn.cursor()
    if category.lower() == "all":
        cur.execute("SELECT name, category, price, available, description, vegetarian FROM menu")
    else:
        cur.execute("SELECT name, category, price, available, description, vegetarian FROM menu WHERE category COLLATE NOCASE = ?", (category.strip(),))
    rows = cur.fetchall()
    conn.close()

    menu_dict = {}
    for r in rows:
        cat = r["category"]
        if cat not in menu_dict:
            menu_dict[cat] = []
        menu_dict[cat].append({
            "name": r["name"],
            "price": r["price"],
            "available": bool(r["available"]),
            "description": r["description"] or "",
            "vegetarian": bool(r["vegetarian"])
        })
    return {"status": "ok", "menu": menu_dict}

def snapshot(session_id: Optional[int]) -> dict:
    if not session_id:
        return {"session": None, "order": {}}
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT session_id, table_no, status, payment_method FROM sessions WHERE session_id = ?", (session_id,))
    s = cur.fetchone()
    conn.close()
    if not s:
        return {"session": None, "order": {}}
    return {
        "session": {"session_id": s["session_id"], "table_no": s["table_no"], "status": s["status"],
                    "payment_method": s["payment_method"]},
        "order": view_current_order(session_id)
    }

TOOL_SPECS = [
    {
        "tool": "add_item_to_order",
        "description": "Add a dish from the menu to the table order.",
        "arguments": {
            "item_name": "Exact name of the menu item",
            "qty": "Quantity (default 1)",
            "instructions": "Optional custom preparation instructions (e.g., 'no onions')"
        }
    },
    {
        "tool": "remove_item_from_order",
        "description": "Remove or reduce quantity of a dish from the order.",
        "arguments": {
            "item_name": "Exact name of the menu item",
            "qty": "Optional quantity to remove"
        }
    },
    {
        "tool": "request_review",
        "description": "Show the itemized bill and review summary to the customer.",
        "arguments": {}
    }
]

LLM_TOOLS = {
    "add_item_to_order": add_item_to_order,
    "remove_item_from_order": remove_item_from_order,
    "request_review": lambda session_id: {"status": "ok"}
}