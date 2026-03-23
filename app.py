import sqlite3
import os
from datetime import date, datetime
from contextlib import contextmanager
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI()

DB_PATH = os.path.join(os.path.dirname(__file__), "expenses.db")

DEFAULT_CATEGORIES = [
    "Food", "Transport", "Shopping", "Bills",
    "Entertainment", "Health", "Education", "Other",
]


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        );
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            date TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (category_id) REFERENCES categories(id)
        );
    """)
    for cat in DEFAULT_CATEGORIES:
        conn.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (cat,))
    conn.commit()
    conn.close()


init_db()

# --- Models ---

class ExpenseIn(BaseModel):
    category_id: int
    amount: int


class CategoryIn(BaseModel):
    name: str


class CategoryUpdate(BaseModel):
    name: str


# --- API: Categories ---

@app.get("/api/categories")
def list_categories():
    conn = get_db()
    rows = conn.execute("SELECT id, name FROM categories ORDER BY name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/categories", status_code=201)
def create_category(cat: CategoryIn):
    conn = get_db()
    try:
        cur = conn.execute("INSERT INTO categories (name) VALUES (?)", (cat.name.strip(),))
        conn.commit()
        cat_id = cur.lastrowid
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(400, "Category already exists")
    conn.close()
    return {"id": cat_id, "name": cat.name.strip()}


@app.put("/api/categories/{cat_id}")
def update_category(cat_id: int, cat: CategoryUpdate):
    conn = get_db()
    try:
        cur = conn.execute("UPDATE categories SET name = ? WHERE id = ?", (cat.name.strip(), cat_id))
        conn.commit()
        if cur.rowcount == 0:
            conn.close()
            raise HTTPException(404, "Category not found")
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(400, "Category name already exists")
    conn.close()
    return {"id": cat_id, "name": cat.name.strip()}


@app.delete("/api/categories/{cat_id}")
def delete_category(cat_id: int):
    conn = get_db()
    # Check if expenses use this category
    count = conn.execute("SELECT COUNT(*) FROM expenses WHERE category_id = ?", (cat_id,)).fetchone()[0]
    if count > 0:
        conn.close()
        raise HTTPException(400, f"Cannot delete: {count} expense(s) use this category")
    cur = conn.execute("DELETE FROM categories WHERE id = ?", (cat_id,))
    conn.commit()
    if cur.rowcount == 0:
        conn.close()
        raise HTTPException(404, "Category not found")
    conn.close()
    return {"ok": True}


# --- API: Expenses ---

@app.post("/api/expenses", status_code=201)
def create_expense(exp: ExpenseIn):
    conn = get_db()
    today = date.today().isoformat()
    cur = conn.execute(
        "INSERT INTO expenses (category_id, amount, date) VALUES (?, ?, ?)",
        (exp.category_id, exp.amount, today),
    )
    conn.commit()
    exp_id = cur.lastrowid
    conn.close()
    return {"id": exp_id, "category_id": exp.category_id, "amount": exp.amount, "date": today}


@app.delete("/api/expenses/{exp_id}")
def delete_expense(exp_id: int):
    conn = get_db()
    cur = conn.execute("DELETE FROM expenses WHERE id = ?", (exp_id,))
    conn.commit()
    if cur.rowcount == 0:
        conn.close()
        raise HTTPException(404, "Expense not found")
    conn.close()
    return {"ok": True}


# --- API: Reports ---

@app.get("/api/report")
def get_report(month: str | None = None):
    """Get expense report. month format: YYYY-MM. Defaults to current month."""
    if month is None:
        month = date.today().strftime("%Y-%m")
    conn = get_db()

    # Summary per category
    summary = conn.execute("""
        SELECT c.name as category, COALESCE(SUM(e.amount), 0) as total
        FROM categories c
        LEFT JOIN expenses e ON e.category_id = c.id AND e.date LIKE ?
        GROUP BY c.id, c.name
        HAVING total > 0
        ORDER BY total DESC
    """, (month + "%",)).fetchall()

    # Detail list
    details = conn.execute("""
        SELECT e.id, c.name as category, e.amount, e.date
        FROM expenses e
        JOIN categories c ON c.id = e.category_id
        WHERE e.date LIKE ?
        ORDER BY e.date DESC, e.id DESC
    """, (month + "%",)).fetchall()

    # Available months
    months = conn.execute("""
        SELECT DISTINCT substr(date, 1, 7) as month FROM expenses ORDER BY month DESC
    """).fetchall()

    conn.close()

    grand_total = sum(r["total"] for r in summary)
    return {
        "month": month,
        "summary": [dict(r) for r in summary],
        "details": [dict(r) for r in details],
        "grand_total": grand_total,
        "available_months": [r["month"] for r in months],
    }


# --- Serve frontend ---

@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(os.path.dirname(__file__), "static", "index.html")) as f:
        return f.read()


app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")
