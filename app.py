import os
from datetime import date, datetime
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import psycopg
from psycopg.rows import dict_row

app = FastAPI()

DATABASE_URL = os.environ.get("DATABASE_URL")

DEFAULT_CATEGORIES = [
    "Food", "Transport", "Shopping", "Bills",
    "Entertainment", "Health", "Education", "Other",
]


def get_db():
    conn = psycopg.connect(DATABASE_URL)
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS categories (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL UNIQUE
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS expenses (
            id SERIAL PRIMARY KEY,
            category_id INTEGER NOT NULL REFERENCES categories(id),
            amount INTEGER NOT NULL,
            date TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT NOW()
        );
    """)
    for cat in DEFAULT_CATEGORIES:
        cur.execute("INSERT INTO categories (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (cat,))
    conn.commit()
    cur.close()
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
    cur = conn.cursor(row_factory=dict_row)
    cur.execute("SELECT id, name FROM categories ORDER BY name")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


@app.post("/api/categories", status_code=201)
def create_category(cat: CategoryIn):
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO categories (name) VALUES (%s) RETURNING id", (cat.name.strip(),))
        cat_id = cur.fetchone()[0]
        conn.commit()
    except psycopg.errors.UniqueViolation:
        conn.rollback()
        cur.close()
        conn.close()
        raise HTTPException(400, "Category already exists")
    cur.close()
    conn.close()
    return {"id": cat_id, "name": cat.name.strip()}


@app.put("/api/categories/{cat_id}")
def update_category(cat_id: int, cat: CategoryUpdate):
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("UPDATE categories SET name = %s WHERE id = %s", (cat.name.strip(), cat_id))
        conn.commit()
        if cur.rowcount == 0:
            cur.close()
            conn.close()
            raise HTTPException(404, "Category not found")
    except psycopg.errors.UniqueViolation:
        conn.rollback()
        cur.close()
        conn.close()
        raise HTTPException(400, "Category name already exists")
    cur.close()
    conn.close()
    return {"id": cat_id, "name": cat.name.strip()}


@app.delete("/api/categories/{cat_id}")
def delete_category(cat_id: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM expenses WHERE category_id = %s", (cat_id,))
    count = cur.fetchone()[0]
    if count > 0:
        cur.close()
        conn.close()
        raise HTTPException(400, f"Cannot delete: {count} expense(s) use this category")
    cur.execute("DELETE FROM categories WHERE id = %s", (cat_id,))
    conn.commit()
    if cur.rowcount == 0:
        cur.close()
        conn.close()
        raise HTTPException(404, "Category not found")
    cur.close()
    conn.close()
    return {"ok": True}


# --- API: Expenses ---

@app.post("/api/expenses", status_code=201)
def create_expense(exp: ExpenseIn):
    conn = get_db()
    cur = conn.cursor()
    today = date.today().isoformat()
    cur.execute(
        "INSERT INTO expenses (category_id, amount, date) VALUES (%s, %s, %s) RETURNING id",
        (exp.category_id, exp.amount, today),
    )
    exp_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return {"id": exp_id, "category_id": exp.category_id, "amount": exp.amount, "date": today}


@app.delete("/api/expenses/{exp_id}")
def delete_expense(exp_id: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM expenses WHERE id = %s", (exp_id,))
    conn.commit()
    if cur.rowcount == 0:
        cur.close()
        conn.close()
        raise HTTPException(404, "Expense not found")
    cur.close()
    conn.close()
    return {"ok": True}


# --- API: Reports ---

@app.get("/api/report")
def get_report(month: str | None = None):
    """Get expense report. month format: YYYY-MM. Defaults to current month."""
    if month is None:
        month = date.today().strftime("%Y-%m")
    conn = get_db()
    cur = conn.cursor(row_factory=dict_row)

    # Summary per category
    cur.execute("""
        SELECT c.name as category, COALESCE(SUM(e.amount), 0) as total
        FROM categories c
        LEFT JOIN expenses e ON e.category_id = c.id AND e.date LIKE %s
        GROUP BY c.id, c.name
        HAVING COALESCE(SUM(e.amount), 0) > 0
        ORDER BY total DESC
    """, (month + "%",))
    summary = cur.fetchall()

    # Detail list
    cur.execute("""
        SELECT e.id, c.name as category, e.amount, e.date
        FROM expenses e
        JOIN categories c ON c.id = e.category_id
        WHERE e.date LIKE %s
        ORDER BY e.date DESC, e.id DESC
    """, (month + "%",))
    details = cur.fetchall()

    # Available months
    cur.execute("""
        SELECT DISTINCT substr(date, 1, 7) as month FROM expenses ORDER BY month DESC
    """)
    months = cur.fetchall()

    cur.close()
    conn.close()

    grand_total = sum(r["total"] for r in summary)
    return {
        "month": month,
        "summary": summary,
        "details": details,
        "grand_total": grand_total,
        "available_months": [r["month"] for r in months],
    }


# --- Serve frontend ---

@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(os.path.dirname(__file__), "static", "index.html")) as f:
        return f.read()


app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")
