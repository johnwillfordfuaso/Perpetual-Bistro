-- ============================================================
-- init_restaurant_db.sql
-- Perpetual Bistro - Virtual Waiter (AI Lab Activity 6.1)
-- Safe to run repeatedly: tables use IF NOT EXISTS and the menu
-- seed uses INSERT OR IGNORE (it will NOT reset stock levels;
-- use `--reset-db` in virtual_waiter_prototype.py for that).
-- ============================================================

PRAGMA foreign_keys = ON;

-- ------------------------------------------------------------
-- menu_items: what the kitchen sells and how much is left
-- allergens: comma-separated list, or 'none'
-- is_vegetarian: 1 / 0 (lets the agent answer "make it vegetarian"
--                from data instead of guessing)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS menu_items (
    id             INTEGER PRIMARY KEY,
    name           TEXT    NOT NULL UNIQUE,
    category       TEXT    NOT NULL,
    price          REAL    NOT NULL CHECK (price >= 0),
    stock_qty      INTEGER NOT NULL DEFAULT 0 CHECK (stock_qty >= 0),
    allergens      TEXT    NOT NULL DEFAULT 'none',
    is_vegetarian  INTEGER NOT NULL DEFAULT 0 CHECK (is_vegetarian IN (0, 1))
);

-- ------------------------------------------------------------
-- table_sessions: one row per customer visit (FSM session)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS table_sessions (
    session_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    table_no      INTEGER NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'open'
                  CHECK (status IN ('open', 'confirmed', 'cancelled')),
    created_at    TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    confirmed_at  TEXT
);

-- ------------------------------------------------------------
-- order_line_items: the live tab. Rows are "pending" while the
-- session is open; stock is only deducted when the order is
-- confirmed (STATE_CONFIRMED).
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS order_line_items (
    line_id               INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id            INTEGER NOT NULL
                          REFERENCES table_sessions(session_id) ON DELETE CASCADE,
    item_id               INTEGER NOT NULL REFERENCES menu_items(id),
    quantity              INTEGER NOT NULL CHECK (quantity > 0),
    special_instructions  TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_lines_session ON order_line_items(session_id);

-- ------------------------------------------------------------
-- Seed data (prices in PHP). Onion Rings start sold out on
-- purpose so the out-of-stock path can be demonstrated.
-- ------------------------------------------------------------
INSERT OR IGNORE INTO menu_items (id, name, category, price, stock_qty, allergens, is_vegetarian) VALUES
    ( 1, 'Cheeseburger',        'Mains',    180.00, 20, 'gluten,dairy,egg', 0),
    ( 2, 'Veggie Burger',       'Mains',    170.00, 15, 'gluten,soy',       1),
    ( 3, 'Spaghetti Bolognese', 'Mains',    220.00, 12, 'gluten,egg',       0),
    ( 4, 'Chicken Adobo Rice',  'Mains',    160.00, 18, 'soy',              0),
    ( 5, 'Pesto Pasta',         'Mains',    200.00,  3, 'gluten,dairy,nuts',1),
    ( 6, 'French Fries',        'Sides',     80.00, 30, 'none',             1),
    ( 7, 'Garden Salad',        'Sides',     95.00, 10, 'none',             1),
    ( 8, 'Onion Rings',         'Sides',     90.00,  0, 'gluten,egg',       1),
    ( 9, 'Iced Tea',            'Drinks',    50.00, 40, 'none',             1),
    (10, 'Mango Smoothie',      'Drinks',    90.00, 15, 'dairy',            1),
    (11, 'Cola',                'Drinks',    45.00, 40, 'none',             1),
    (12, 'Bottled Water',       'Drinks',    30.00, 50, 'none',             1),
    (13, 'Leche Flan',          'Desserts',  85.00,  8, 'dairy,egg',        1),
    (14, 'Halo-Halo',           'Desserts', 110.00,  6, 'dairy',            1);
