import os
import re
from datetime import date, datetime, timedelta, timezone
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import psycopg
from psycopg.rows import dict_row
from passlib.hash import bcrypt
import jwt

app = FastAPI()

DATABASE_URL = os.environ.get("DATABASE_URL")
JWT_SECRET = os.environ.get("JWT_SECRET", "change-me-in-production")

DEFAULT_CATEGORIES = [
    "Food", "Transport", "Shopping", "Bills",
    "Entertainment", "Health", "Education", "Other",
]


def get_db():
    conninfo = DATABASE_URL
    if not conninfo:
        raise RuntimeError("DATABASE_URL environment variable is not set")
    if "sslmode" not in conninfo:
        sep = "&" if "?" in conninfo else "?"
        conninfo += sep + "sslmode=require"
    conn = psycopg.connect(conninfo)
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT NOW()
        );
    """)
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
            description TEXT NOT NULL DEFAULT '',
            date TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT NOW()
        );
    """)
    # Migrations for existing DBs
    cur.execute("ALTER TABLE expenses ADD COLUMN IF NOT EXISTS description TEXT NOT NULL DEFAULT '';")
    cur.execute("ALTER TABLE expenses ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id);")
    for cat in DEFAULT_CATEGORIES:
        cur.execute("INSERT INTO categories (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (cat,))
    conn.commit()
    cur.close()
    conn.close()


try:
    init_db()
    print("Database initialized successfully")
except Exception as e:
    print(f"ERROR: init_db failed: {e}")
    import traceback
    traceback.print_exc()


# --- Auth Utilities ---

def hash_password(plain: str) -> str:
    return bcrypt.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.verify(plain, hashed)


def create_token(user_id: int, username: str) -> str:
    payload = {
        "sub": user_id,
        "username": username,
        "exp": datetime.now(timezone.utc) + timedelta(days=7),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def get_current_user(request: Request) -> dict:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Not authenticated")
    token = auth[7:]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return {"id": payload["sub"], "username": payload["username"]}
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid token")


# --- Models ---

class ExpenseIn(BaseModel):
    category_id: int
    amount: int
    description: str = ""


class CategoryIn(BaseModel):
    name: str


class CategoryUpdate(BaseModel):
    name: str


class AuthIn(BaseModel):
    username: str
    password: str


# --- API: Auth ---

@app.post("/api/register", status_code=201)
def register(body: AuthIn):
    username = body.username.strip()
    if not re.match(r'^[a-zA-Z0-9_]{3,30}$', username):
        raise HTTPException(400, "Username must be 3-30 characters (letters, numbers, underscores)")
    if len(body.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO users (username, password_hash) VALUES (%s, %s) RETURNING id",
            (username, hash_password(body.password)),
        )
        user_id = cur.fetchone()[0]
        conn.commit()
    except psycopg.errors.UniqueViolation:
        conn.rollback()
        cur.close()
        conn.close()
        raise HTTPException(400, "Username already taken")
    cur.close()
    conn.close()
    return {"token": create_token(user_id, username), "username": username}


@app.post("/api/login")
def login(body: AuthIn):
    conn = get_db()
    cur = conn.cursor(row_factory=dict_row)
    cur.execute("SELECT id, username, password_hash FROM users WHERE username = %s", (body.username.strip(),))
    user = cur.fetchone()
    cur.close()
    conn.close()
    if not user or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(401, "Invalid username or password")
    return {"token": create_token(user["id"], user["username"]), "username": user["username"]}


# --- API: Categories (global, no auth required) ---

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


# --- API: Expenses (auth required, user-scoped) ---

@app.post("/api/expenses", status_code=201)
def create_expense(exp: ExpenseIn, request: Request):
    user = get_current_user(request)
    conn = get_db()
    cur = conn.cursor()
    today = date.today().isoformat()
    cur.execute(
        "INSERT INTO expenses (category_id, amount, description, date, user_id) VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (exp.category_id, exp.amount, exp.description.strip(), today, user["id"]),
    )
    exp_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return {"id": exp_id, "category_id": exp.category_id, "amount": exp.amount, "description": exp.description.strip(), "date": today}


@app.delete("/api/expenses/{exp_id}")
def delete_expense(exp_id: int, request: Request):
    user = get_current_user(request)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM expenses WHERE id = %s AND user_id = %s", (exp_id, user["id"]))
    conn.commit()
    if cur.rowcount == 0:
        cur.close()
        conn.close()
        raise HTTPException(404, "Expense not found")
    cur.close()
    conn.close()
    return {"ok": True}


# --- API: Reports (auth required, user-scoped) ---

def _build_report(user_id: int, month: str):
    """Build report data for a given user and month."""
    conn = get_db()
    cur = conn.cursor(row_factory=dict_row)

    cur.execute("""
        SELECT c.name as category, COALESCE(SUM(e.amount), 0) as total
        FROM categories c
        LEFT JOIN expenses e ON e.category_id = c.id AND e.date LIKE %s AND e.user_id = %s
        GROUP BY c.id, c.name
        HAVING COALESCE(SUM(e.amount), 0) > 0
        ORDER BY total DESC
    """, (month + "%", user_id))
    summary = cur.fetchall()

    cur.execute("""
        SELECT e.id, c.name as category, e.amount, e.description, e.date
        FROM expenses e
        JOIN categories c ON c.id = e.category_id
        WHERE e.date LIKE %s AND e.user_id = %s
        ORDER BY e.date DESC, e.id DESC
    """, (month + "%", user_id))
    details = cur.fetchall()

    cur.execute("""
        SELECT DISTINCT substr(date, 1, 7) as month FROM expenses WHERE user_id = %s ORDER BY month DESC
    """, (user_id,))
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


@app.get("/api/report")
def get_report(request: Request, month: str | None = None):
    user = get_current_user(request)
    if month is None:
        month = date.today().strftime("%Y-%m")
    return _build_report(user["id"], month)


@app.get("/api/report/shared")
def get_shared_report(request: Request, username: str, month: str | None = None):
    get_current_user(request)  # must be logged in
    if month is None:
        month = date.today().strftime("%Y-%m")
    conn = get_db()
    cur = conn.cursor(row_factory=dict_row)
    cur.execute("SELECT id FROM users WHERE username = %s", (username,))
    target = cur.fetchone()
    cur.close()
    conn.close()
    if not target:
        raise HTTPException(404, "User not found")
    report = _build_report(target["id"], month)
    report["username"] = username
    return report


# --- Serve frontend ---

@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(os.path.dirname(__file__), "static", "index.html")) as f:
        return f.read()


app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")
