"""
ENLIGHT TUTOR Backend
======================
FastAPI backend for enlight_rsa.html. Serves many students without any single student
hitting an AI rate limit, on free tiers only, with zero configuration required from
students — plus login accounts and a manual (bank transfer) payment approval system.

Core pieces:

1. FreeFlow LLM (https://pypi.org/project/freeflow-llm) chains multiple FREE-tier AI
   providers (Groq, Gemini, GitHub Models) with automatic fallback on rate limits.

2. Every lesson's notes are cached in SQLite, keyed by (grade, subject, lesson) — the
   first student to open a lesson triggers one AI call, every student after gets it free.

3. Video links cost nothing: no YouTube Data API is used. A precise, topic-scoped
   YouTube search URL is built directly and returned as a "Watch on YouTube" link.

4. Accounts + manual payment approval:
   - Students sign up / log in with email + password (POST /api/auth/signup, /api/auth/login).
   - New accounts start with NO access to AI features (has_access = False).
   - Students pay via Capitec/TymeBank bank transfer (details shown in the frontend) and
     submit proof of payment through the app (POST /api/payment/submit — file upload or a
     note saying it was sent via WhatsApp/email).
   - The creator account reviews pending requests (GET /api/admin/payment-requests) and
     clicks "Grant Access" (POST /api/admin/grant-access), which unlocks that student.
   - The creator's own account (zwanelwazi04@gmail.com) always gets free, permanent,
     admin access when logging in with ANY password ending in "Lwazi" — see
     CREATOR_EMAIL / CREATOR_PASSWORD_SUFFIX below.

   SECURITY NOTE: the creator password-suffix rule is intentionally permissive (any
   password ending in "Lwazi" logs in as the creator/admin). This is convenient but
   means anyone who learns the creator's email and guesses/knows this suffix rule gets
   full admin access. Keep that email private, or replace this rule with a normal fixed
   password before handing the deployed URL to anyone else.

5. Per-IP rate limiting protects pooled AI keys from a single runaway client.

Run locally:
    pip install -r requirements.txt
    cp .env.example .env   # fill in your keys
    uvicorn app:app --reload --port 8000

Deploy: see README.md (Railway / Docker instructions).
"""

import os
import re
import time
import shutil
import secrets
import hashlib
import logging
import sqlite3
import smtplib
from email.mime.text import MIMEText
from collections import defaultdict, deque
from urllib.parse import quote_plus
from urllib.request import urlopen, Request as UrlRequest
from urllib.error import URLError, HTTPError
import json

from fastapi import FastAPI, HTTPException, Request, Depends, Header, UploadFile, File, Form, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from dotenv import load_dotenv

# The app must ALWAYS be able to start and bind $PORT — even if freeflow-llm fails to
# import (missing package, version mismatch, etc.) or fails to initialize (no provider
# keys set yet). If either of those things go wrong, we log it clearly and every AI
# endpoint returns a clean 503 instead of crashing the whole process at startup — a
# crash here is exactly what causes both a Railway "start command failed" AND a health
# check failure, since the server never comes up to bind the port or answer requests.
try:
    from freeflow_llm import FreeFlowClient, NoProvidersAvailableError
    _FREEFLOW_IMPORT_ERROR = None
except Exception as _e:  # pragma: no cover — defensive, see comment above
    FreeFlowClient = None
    class NoProvidersAvailableError(Exception):
        pass
    _FREEFLOW_IMPORT_ERROR = str(_e)

load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("enlight-tutor")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DB_PATH = os.getenv("DB_PATH", "enlight_cache.db")
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "uploads")
RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "20"))
SESSION_DAYS_VALID = int(os.getenv("SESSION_DAYS_VALID", "30"))

_raw_origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
ALLOWED_ORIGINS = _raw_origins if _raw_origins else ["*"]

# The creator's account: logging in with this email and ANY password ending in this
# suffix always grants free, permanent, admin access. See SECURITY NOTE above.
CREATOR_EMAIL = os.getenv("CREATOR_EMAIL", "zwanelwazi04@gmail.com").strip().lower()
CREATOR_PASSWORD_SUFFIX = os.getenv("CREATOR_PASSWORD_SUFFIX", "4560Lwazi#Zwane")

# Optional: enables "Forgot Password" emails. If left blank, forgot-password requests are
# accepted (so the API doesn't leak which emails exist) but no email actually goes out —
# a warning is logged instead so you can see it happened.
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER)
FRONTEND_URL = os.getenv("FRONTEND_URL", "").rstrip("/")

os.makedirs(UPLOAD_DIR, exist_ok=True)

app = FastAPI(title="ENLIGHT TUTOR Backend", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Database (SQLite, WAL mode for concurrent request safety)
# ---------------------------------------------------------------------------
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.execute("PRAGMA journal_mode=WAL")
_conn.execute("PRAGMA busy_timeout=5000")

_conn.execute(
    "CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT NOT NULL, created_at REAL NOT NULL)"
)
_conn.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        password_salt TEXT NOT NULL,
        has_access INTEGER NOT NULL DEFAULT 0,
        is_admin INTEGER NOT NULL DEFAULT 0,
        free_forever INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL
    )
""")
_conn.execute("""
    CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL
    )
""")
try:
    _conn.execute("ALTER TABLE sessions ADD COLUMN ip_address TEXT")
except sqlite3.OperationalError:
    pass  # column already exists (safe to run repeatedly, including against older DB files)
_conn.execute("""
    CREATE TABLE IF NOT EXISTS password_resets (
        token TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        used INTEGER NOT NULL DEFAULT 0
    )
""")
_conn.execute("""
    CREATE TABLE IF NOT EXISTS payment_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        email TEXT NOT NULL,
        method TEXT NOT NULL,
        reference TEXT,
        proof_filename TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at REAL NOT NULL,
        reviewed_at REAL
    )
""")
_conn.commit()


def cache_key(*parts: str) -> str:
    raw = "::".join(str(p).strip().lower() for p in parts)
    return re.sub(r"\s+", "_", raw)


def cache_get(key: str):
    row = _conn.execute("SELECT value FROM cache WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def cache_set(key: str, value: str):
    _conn.execute(
        "INSERT OR REPLACE INTO cache (key, value, created_at) VALUES (?, ?, ?)",
        (key, value, time.time()),
    )
    _conn.commit()


# ---------------------------------------------------------------------------
# Password hashing (PBKDF2-HMAC-SHA256, stdlib only — no extra dependency needed)
# ---------------------------------------------------------------------------
def hash_password(password: str, salt: str = None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100_000)
    return digest.hex(), salt


def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    computed, _ = hash_password(password, salt)
    return secrets.compare_digest(computed, expected_hash)


# ---------------------------------------------------------------------------
# User + session helpers
# ---------------------------------------------------------------------------
USER_COLUMNS = "id, email, password_hash, password_salt, has_access, is_admin, free_forever"


def get_user_row_by_email(email: str):
    return _conn.execute(
        f"SELECT {USER_COLUMNS} FROM users WHERE email = ?", (email.strip().lower(),)
    ).fetchone()


def get_user_row_by_id(user_id: int):
    return _conn.execute(f"SELECT {USER_COLUMNS} FROM users WHERE id = ?", (user_id,)).fetchone()


def create_user_row(email: str, password: str, has_access=False, is_admin=False, free_forever=False):
    pw_hash, pw_salt = hash_password(password)
    _conn.execute(
        "INSERT INTO users (email, password_hash, password_salt, has_access, is_admin, free_forever, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (email.strip().lower(), pw_hash, pw_salt, int(has_access), int(is_admin), int(free_forever), time.time()),
    )
    _conn.commit()
    return get_user_row_by_email(email)


def create_session(user_id: int, ip_address: str = None) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    _conn.execute(
        "INSERT INTO sessions (token, user_id, created_at, expires_at, ip_address) VALUES (?, ?, ?, ?, ?)",
        (token, user_id, now, now + SESSION_DAYS_VALID * 86400, ip_address),
    )
    _conn.commit()
    return token


def send_email(to_email: str, subject: str, body: str) -> bool:
    if not (SMTP_HOST and SMTP_USER and SMTP_PASSWORD):
        logger.warning(f"Email NOT sent to {to_email} (SMTP not configured): {subject}")
        return False
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = SMTP_FROM
        msg["To"] = to_email
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM, [to_email], msg.as_string())
        logger.info(f"Email sent to {to_email}: {subject}")
        return True
    except Exception as e:
        logger.error(f"Failed to send email to {to_email}: {e}")
        return False


def user_row_to_dict(row) -> dict:
    return {
        "id": row[0],
        "email": row[1],
        "has_access": bool(row[4]),
        "is_admin": bool(row[5]),
        "free_forever": bool(row[6]),
    }


def resolve_token(token: str):
    row = _conn.execute("SELECT user_id, expires_at FROM sessions WHERE token = ?", (token,)).fetchone()
    if not row:
        return None
    user_id, expires_at = row
    if expires_at < time.time():
        return None
    user_row = get_user_row_by_id(user_id)
    return user_row_to_dict(user_row) if user_row else None


def get_current_user(authorization: str = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Please log in to continue.")
    token = authorization.split(" ", 1)[1].strip()
    user = resolve_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Your session has expired — please log in again.")
    return user


def get_current_user_flexible(authorization: str = Header(None), token: str = Query(None)) -> dict:
    """Same as get_current_user, but also accepts ?token= for use in plain <a href> links
    (e.g. viewing a proof-of-payment file in a new tab, where custom headers can't be set)."""
    raw_token = None
    if authorization and authorization.startswith("Bearer "):
        raw_token = authorization.split(" ", 1)[1].strip()
    elif token:
        raw_token = token
    if not raw_token:
        raise HTTPException(status_code=401, detail="Please log in to continue.")
    user = resolve_token(raw_token)
    if not user:
        raise HTTPException(status_code=401, detail="Your session has expired — please log in again.")
    return user


def require_access(user: dict = Depends(get_current_user)) -> dict:
    if not (user["has_access"] or user["free_forever"] or user["is_admin"]):
        raise HTTPException(
            status_code=402,
            detail="Payment required. Please complete payment and submit proof to unlock AI features.",
        )
    return user


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Admin access required.")
    return user


def require_admin_flexible(user: dict = Depends(get_current_user_flexible)) -> dict:
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Admin access required.")
    return user


# ---------------------------------------------------------------------------
# Simple in-memory sliding-window rate limiter, per client IP.
# NOTE: this is per-process. If you scale to multiple backend replicas, move this to a
# shared store (e.g. Redis) — otherwise each replica enforces its own limit independently.
# ---------------------------------------------------------------------------
_hits: dict = defaultdict(deque)


def enforce_rate_limit(request: Request):
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    dq = _hits[ip]
    while dq and now - dq[0] > 60:
        dq.popleft()
    if len(dq) >= RATE_LIMIT_PER_MIN:
        raise HTTPException(status_code=429, detail="Too many requests — please slow down and try again shortly.")
    dq.append(now)


# ---------------------------------------------------------------------------
# Shared AI client — created once at startup and reused across requests.
# NEVER let this line crash the process — see comment on the freeflow_llm import above.
# ---------------------------------------------------------------------------
_ai_client = None
_ai_client_error = _FREEFLOW_IMPORT_ERROR
if FreeFlowClient is not None:
    try:
        _ai_client = FreeFlowClient()
    except Exception as _e:
        _ai_client_error = str(_e)
_configured_providers: list = []


def get_ai_client():
    """Use inside endpoints instead of touching _ai_client directly, so a missing/broken
    freeflow-llm setup returns a clean, informative error instead of an unhandled crash."""
    if _ai_client is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "AI engine isn't available on this server right now"
                + (f" ({_ai_client_error})" if _ai_client_error else "")
                + ". Check that freeflow-llm is installed and at least one provider key "
                "(GROQ_API_KEY / GEMINI_API_KEY / GITHUB_TOKEN) is set."
            ),
        )
    return _ai_client


# =============================================================================
# AUTH
# =============================================================================
class SignupRequest(BaseModel):
    email: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


@app.post("/api/auth/signup")
def signup(req: SignupRequest, request: Request):
    email = req.email.strip().lower()
    if not email or "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(status_code=400, detail="Please enter a valid email address.")
    if len(req.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters.")
    if get_user_row_by_email(email):
        raise HTTPException(status_code=409, detail="An account with this email already exists — please log in instead.")

    ip = request.client.host if request.client else None
    is_creator = email == CREATOR_EMAIL and req.password.endswith(CREATOR_PASSWORD_SUFFIX)
    user_row = create_user_row(email, req.password, has_access=is_creator, is_admin=is_creator, free_forever=is_creator)
    token = create_session(user_row[0], ip_address=ip)
    logger.info(f"signup: new user {email}" + (" (creator)" if is_creator else ""))
    result = user_row_to_dict(user_row)
    result["token"] = token
    return result


@app.post("/api/auth/login")
def login(req: LoginRequest, request: Request):
    email = req.email.strip().lower()
    password = req.password
    ip = request.client.host if request.client else None

    # Creator bypass: this exact email + ANY password ending in the configured suffix
    # always logs in as a free-forever admin account (created on first use if needed).
    if email == CREATOR_EMAIL and password.endswith(CREATOR_PASSWORD_SUFFIX):
        existing = get_user_row_by_email(email)
        if not existing:
            existing = create_user_row(email, password, has_access=True, is_admin=True, free_forever=True)
        else:
            _conn.execute("UPDATE users SET has_access=1, is_admin=1, free_forever=1 WHERE id=?", (existing[0],))
            _conn.commit()
            existing = get_user_row_by_id(existing[0])
        token = create_session(existing[0], ip_address=ip)
        logger.info(f"login: creator account {email}")
        result = user_row_to_dict(existing)
        result["token"] = token
        return result

    existing = get_user_row_by_email(email)
    if not existing:
        raise HTTPException(status_code=401, detail="No account found with this email — please sign up first.")
    _, _, pw_hash, pw_salt, *_ = existing
    if not verify_password(password, pw_salt, pw_hash):
        raise HTTPException(status_code=401, detail="Incorrect password.")
    token = create_session(existing[0], ip_address=ip)
    result = user_row_to_dict(existing)
    result["token"] = token
    return result


@app.get("/api/auth/me")
def me(user: dict = Depends(get_current_user)):
    return user


@app.post("/api/auth/logout")
def logout_current(authorization: str = Header(None), user: dict = Depends(get_current_user)):
    """Invalidate THIS device's session token server-side (not just clearing it client-side)."""
    token = authorization.split(" ", 1)[1].strip()
    _conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
    _conn.commit()
    return {"status": "logged_out"}


@app.post("/api/auth/logout-others")
def logout_others(authorization: str = Header(None), user: dict = Depends(get_current_user)):
    """Log out every OTHER device/browser for this account, keeping the current session active."""
    token = authorization.split(" ", 1)[1].strip()
    result = _conn.execute("DELETE FROM sessions WHERE user_id = ? AND token != ?", (user["id"], token))
    _conn.commit()
    logger.info(f"{user['email']}: logged out {result.rowcount} other session(s)")
    return {"status": "other_sessions_logged_out", "count": result.rowcount}


@app.get("/api/auth/sessions")
def list_sessions(authorization: str = Header(None), user: dict = Depends(get_current_user)):
    """List this account's active login sessions (for a 'log out other devices' UI)."""
    token = authorization.split(" ", 1)[1].strip()
    rows = _conn.execute(
        "SELECT token, created_at, expires_at, ip_address FROM sessions WHERE user_id = ? ORDER BY created_at DESC",
        (user["id"],),
    ).fetchall()
    return [
        {"created_at": r[1], "expires_at": r[2], "ip_address": r[3], "is_current": r[0] == token}
        for r in rows
    ]


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@app.post("/api/auth/change-password")
def change_password(req: ChangePasswordRequest, user: dict = Depends(get_current_user)):
    if len(req.new_password) < 6:
        raise HTTPException(status_code=400, detail="New password must be at least 6 characters.")
    row = get_user_row_by_id(user["id"])
    _, email, pw_hash, pw_salt, *_ = row
    if email == CREATOR_EMAIL:
        raise HTTPException(
            status_code=400,
            detail="The creator account always logs in via its configured password-suffix rule — there's no separate password to change here.",
        )
    if not verify_password(req.current_password, pw_salt, pw_hash):
        raise HTTPException(status_code=401, detail="Current password is incorrect.")
    new_hash, new_salt = hash_password(req.new_password)
    _conn.execute("UPDATE users SET password_hash = ?, password_salt = ? WHERE id = ?", (new_hash, new_salt, user["id"]))
    _conn.commit()
    logger.info(f"{email}: password changed")
    return {"status": "password_changed"}


class ForgotPasswordRequest(BaseModel):
    email: str


@app.post("/api/auth/forgot-password")
def forgot_password(req: ForgotPasswordRequest):
    email = req.email.strip().lower()
    generic_response = {"status": "ok", "message": "If an account exists for this email, a reset link has been sent."}
    user = get_user_row_by_email(email)
    # Always return the same generic response whether or not the account exists, so this
    # endpoint can't be used to check which emails are registered.
    if not user or email == CREATOR_EMAIL:
        # Creator account never needs email reset — the password-suffix rule always works.
        return generic_response

    token = secrets.token_urlsafe(32)
    now = time.time()
    _conn.execute(
        "INSERT INTO password_resets (token, user_id, created_at, expires_at, used) VALUES (?, ?, ?, ?, 0)",
        (token, user[0], now, now + 3600),
    )
    _conn.commit()
    reset_link = f"{FRONTEND_URL}?reset_token={token}" if FRONTEND_URL else f"reset_token={token} (set FRONTEND_URL in .env to build a real link)"
    send_email(
        email,
        "Reset your ENLIGHT RSA password",
        "Click the link below to set a new password. This link expires in 1 hour and can only be used once.\n\n"
        f"{reset_link}\n\nIf you didn't request this, you can safely ignore this email.",
    )
    return generic_response


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


@app.post("/api/auth/reset-password")
def reset_password(req: ResetPasswordRequest):
    if len(req.new_password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters.")
    row = _conn.execute(
        "SELECT user_id, expires_at, used FROM password_resets WHERE token = ?", (req.token,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=400, detail="Invalid reset link.")
    user_id, expires_at, used = row
    if used or expires_at < time.time():
        raise HTTPException(status_code=400, detail="This reset link has expired or was already used — please request a new one.")
    pw_hash, pw_salt = hash_password(req.new_password)
    _conn.execute("UPDATE users SET password_hash = ?, password_salt = ? WHERE id = ?", (pw_hash, pw_salt, user_id))
    _conn.execute("UPDATE password_resets SET used = 1 WHERE token = ?", (req.token,))
    # For safety, log the account out everywhere once its password has been reset.
    _conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    _conn.commit()
    logger.info(f"password reset completed for user_id={user_id}")
    return {"status": "password_reset"}


# =============================================================================
# PAYMENT (manual bank transfer + proof of payment)
# =============================================================================
@app.post("/api/payment/submit")
async def submit_payment(
    method: str = Form(...),
    reference: str = Form(""),
    proof: UploadFile = File(None),
    user: dict = Depends(get_current_user),
):
    filename = None
    if proof is not None and proof.filename:
        ext = os.path.splitext(proof.filename)[1][:10]
        filename = f"user{user['id']}_{int(time.time())}{ext}"
        dest_path = os.path.join(UPLOAD_DIR, filename)
        with open(dest_path, "wb") as f:
            shutil.copyfileobj(proof.file, f)

    _conn.execute(
        "INSERT INTO payment_requests (user_id, email, method, reference, proof_filename, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
        (user["id"], user["email"], method, reference, filename, time.time()),
    )
    _conn.commit()
    logger.info(f"payment request submitted by {user['email']} via {method}")
    return {"status": "submitted"}


# =============================================================================
# ADMIN (creator-only: review pending payments, grant access)
# =============================================================================
@app.get("/api/admin/payment-requests")
def list_payment_requests(status: str = "pending", admin: dict = Depends(require_admin)):
    rows = _conn.execute(
        "SELECT id, user_id, email, method, reference, proof_filename, status, created_at "
        "FROM payment_requests WHERE status = ? ORDER BY created_at DESC",
        (status,),
    ).fetchall()
    return [
        {
            "id": r[0],
            "user_id": r[1],
            "email": r[2],
            "method": r[3],
            "reference": r[4],
            "has_proof": bool(r[5]),
            "status": r[6],
            "created_at": r[7],
        }
        for r in rows
    ]


@app.get("/api/admin/proof/{request_id}")
def get_proof(request_id: int, admin: dict = Depends(require_admin_flexible)):
    row = _conn.execute("SELECT proof_filename FROM payment_requests WHERE id = ?", (request_id,)).fetchone()
    if not row or not row[0]:
        raise HTTPException(status_code=404, detail="No proof file was uploaded for this request.")
    path = os.path.join(UPLOAD_DIR, row[0])
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Proof file not found on the server.")
    return FileResponse(path)


class GrantAccessRequest(BaseModel):
    request_id: int = None
    email: str = None


@app.post("/api/admin/grant-access")
def grant_access(req: GrantAccessRequest, admin: dict = Depends(require_admin)):
    target_email = None
    if req.request_id:
        row = _conn.execute("SELECT email FROM payment_requests WHERE id = ?", (req.request_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Payment request not found.")
        target_email = row[0]
        _conn.execute(
            "UPDATE payment_requests SET status = 'approved', reviewed_at = ? WHERE id = ?",
            (time.time(), req.request_id),
        )
    elif req.email:
        target_email = req.email.strip().lower()
    else:
        raise HTTPException(status_code=400, detail="Provide a request_id or an email.")

    target_user = get_user_row_by_email(target_email)
    if not target_user:
        raise HTTPException(status_code=404, detail=f"No account found for {target_email}.")

    _conn.execute("UPDATE users SET has_access = 1 WHERE id = ?", (target_user[0],))
    _conn.commit()
    logger.info(f"admin {admin['email']} granted access to {target_email}")
    return {"status": "granted", "email": target_email}


# =============================================================================
# AI: lesson notes (cached, CAPS-formatted explained summary of ONE specific lesson)
# Requires payment access (has_access / free_forever / is_admin).
# =============================================================================
LESSON_PROMPT = """Generate a comprehensive, DETAILED Grade {grade} CAPS lesson for the subject "{subject}" on the topic: "{lesson}".

DEPTH REQUIREMENT — this is not a summary, it is a full teaching lesson:
- Do NOT write a brief overview. Expand every section fully. Where a summary would give one
  sentence, give a full paragraph. Where it would list 3 bullet points, give 6-10 if the topic
  supports it. Assume the student has never seen this topic before and needs everything
  explained from first principles up to exam-ready mastery.
- The lesson MUST include ALL of the following sections, in this order, and none may be skipped:
  1. <h2>Topic Name</h2>
  2. <h3>Key Concepts</h3> — every core idea in this topic, each explained in its own paragraph,
     not just named.
  3. <h3>Definitions</h3> — every term/definition/law relevant to this topic, each in its own
     <blockquote>, with the definition explained in plain language immediately after it.
  4. <h3>What It Means</h3> — the intuitive, plain-language explanation of the topic.
  5. <h3>Key Formula(s)</h3> (if applicable) — every relevant formula, each in its own
     <div class="formula-box">, with a short explanation of what each symbol/variable means.
  6. <h3>Critical Things to Observe</h3> — common mistakes, easily-confused cases, conditions
     that must hold for a rule to apply, and exam pitfalls, each as its own list item or
     <div class="key-point">. Be specific and exhaustive, not generic ("be careful") advice.
  7. <h3>Worked Examples</h3> — at least 2 full worked examples of increasing difficulty, each
     in its own <div class="example-box"> with <h4>Problem:</h4> and <h4>Solution:</h4>, fully
     step-by-step and numbered.
  8. <h3>Real-World Examples</h3> (where relevant) — concrete applications.
  9. <h3>Summary</h3> — a genuine recap of everything covered above (this is the ONLY place a
     condensed list is appropriate — everywhere else must be fully expanded).

MATHEMATICAL NOTATION — strictly enforced:
- Powers/exponents MUST use real superscript characters: x\u00b2, x\u00b3, x\u2074, a\u207f, etc.
  NEVER write x^2, x**2, x^n, or "x to the power of 2" in running text — always the superscript
  character itself (e.g. "x\u00b2" not "x^2").
- Use brackets/parentheses correctly and ONLY where mathematically required for grouping or
  order of operations — e.g. "2(x + 3)" is correct, "2(x)+3" or unnecessary nested brackets are
  not. Never leave a bracket unclosed or add brackets that change nothing.
- Use \u00d7 for multiplication (not x or *) and \u00f7 for division where appropriate in formulas.
- Use proper subscripts where standard (e.g. a\u2081, x\u2099) rather than a_1, x_n in running text.

FORMATTING RULES:
- Use <h3> for the required major sections above, <h4> for subsections within them.
- Use <blockquote> for definitions/laws.
- Use <div class="formula-box"> for formulas/equations.
- Use <div class="key-point"> for critical takeaways/warnings.
- Use <div class="example-box"> for worked examples, with <h4>Problem:</h4> and <h4>Solution:</h4>.
- Use <table> with <thead>/<tbody> for comparisons, conditions, or summaries.
- Use <ul> with plain list items (no custom bullets — CSS handles styling).
- Use <strong> for emphasis on key terms, <code> for units/variables/short inline formulas.
- Use <hr> to separate major sections.
- Include step-by-step numbered solutions for every worked example.

VISUAL DIAGRAMS — this is required whenever the topic has a visual/spatial/structural
component (geometry theorems, circle theorems, graphs, forces/vectors, biology structures,
cell/organ diagrams, chemical structures, cycles, flowcharts, maps, timelines, etc.):

- Draw the diagram yourself as inline SVG, wrapped exactly like this:
  <div class="diagram-box">
    <svg viewBox="0 0 400 300" xmlns="http://www.w3.org/2000/svg">
      <!-- shapes, lines, labels here -->
    </svg>
    <p class="diagram-caption">One sentence describing exactly what the diagram shows.</p>
  </div>
- Give the SVG a white background rectangle first (<rect width="400" height="300" fill="#ffffff"/>)
  so it stays readable on a dark page, THEN draw on top of it in dark, high-contrast colors
  (e.g. #1a1a1a for lines/labels, and 2-3 accent colors like #2563eb, #dc2626, #16a34a for
  angles/segments/labels being compared or emphasized) — the same style as a printed textbook
  diagram: labeled points (A, B, C, O for centre, etc.), labeled angles, clean geometric lines.
- For circle theorems specifically: draw the actual circle, the actual chord/tangent/angle
  being discussed, label every point, and mark the specific angle(s) the theorem is about
  (e.g. with a small arc and a color) — exactly like a textbook figure, not a generic circle.
- For graphs (functions, data): draw actual labeled x/y axes with a plotted curve/line/bars.
- For biology/science structures: draw simplified labeled diagrams (cell parts, organ
  layout, force arrows, reaction pathways) with text labels next to each part.
- Include 1-3 diagrams per lesson, placed right next to the section they illustrate (e.g.
  the diagram for a theorem goes directly under that theorem's explanation, not all at the end).
- Keep SVGs simple, geometrically accurate, and self-contained (no external images, fonts,
  or scripts inside the SVG) — plain <circle>, <line>, <path>, <polygon>, <text> elements only.

Make it educational, accurate, and strictly aligned with South African CAPS curriculum standards.
Base explanations on standard, widely-used South African textbook conventions and terminology
(the kind found in open/free CAPS textbooks and study guides), not obscure or non-standard notation.
Do NOT include <html>, <head>, or <body> tags."""

LESSON_SYSTEM_PROMPT = (
    "You are ENLIGHTQUANTS AI, an elite South African curriculum developer, senior professor, "
    "and technical illustrator. You NEVER summarize — every lesson you write is fully expanded, "
    "exhaustive, and exam-ready, covering every key concept, every definition, and every "
    "critical thing a student must observe, in full explained detail rather than condensed "
    "bullet points. You are strict about mathematical notation: real superscript characters "
    "for powers (x\u00b2, x\u00b3), never caret or double-asterisk notation, and correct, "
    "minimal use of brackets. You illustrate every visual/spatial concept with your own "
    "accurate inline SVG diagrams in the same clean, labeled style as a printed textbook or "
    "revision poster — clear labeled points, colored angles/segments, and a one-line caption "
    "under each diagram. Think step-by-step, anticipate student misconceptions, provide nuanced "
    "explanations, and structure responses with academic depth."
)


class LessonRequest(BaseModel):
    grade: str
    subject: str
    lesson: str


@app.post("/api/lesson-notes")
def lesson_notes(req: LessonRequest, request: Request, user: dict = Depends(require_access)):
    enforce_rate_limit(request)
    key = "notes::" + cache_key(req.grade, req.subject, req.lesson)
    cached = cache_get(key)
    if cached:
        logger.info(f"lesson-notes cache HIT grade={req.grade} subject={req.subject} lesson={req.lesson}")
        return {"html": cached, "cached": True}

    logger.info(f"lesson-notes cache MISS grade={req.grade} subject={req.subject} lesson={req.lesson} — calling AI")
    prompt = LESSON_PROMPT.format(grade=req.grade, subject=req.subject, lesson=req.lesson)
    client = get_ai_client()
    try:
        response = client.chat(
            messages=[
                {"role": "system", "content": LESSON_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.4,
            max_tokens=6000,  # increased from 4000 — fully-expanded, non-summarized lessons run long
        )
    except NoProvidersAvailableError as e:
        logger.warning(f"lesson-notes: all AI providers exhausted — {e}")
        raise HTTPException(
            status_code=503,
            detail=f"All free AI providers are rate-limited right now, please try again shortly: {e}",
        )

    html = (response.content or "").strip()
    if not html:
        raise HTTPException(status_code=502, detail="AI provider returned an empty response.")
    cache_set(key, html)
    logger.info(f"lesson-notes generated via provider={response.provider}")
    return {"html": html, "cached": False, "provider": response.provider}


# =============================================================================
# AI: general chat (tutor, exam help, etc.). Requires payment access.
# =============================================================================
class ChatRequest(BaseModel):
    messages: list
    temperature: float = 0.7
    max_tokens: int = 2000


@app.post("/api/chat")
def chat(req: ChatRequest, request: Request, user: dict = Depends(require_access)):
    enforce_rate_limit(request)
    client = get_ai_client()
    try:
        response = client.chat(
            messages=req.messages,
            temperature=req.temperature,
            max_tokens=req.max_tokens,
        )
    except NoProvidersAvailableError as e:
        logger.warning(f"chat: all AI providers exhausted — {e}")
        raise HTTPException(
            status_code=503,
            detail=f"All free AI providers are rate-limited right now, please try again shortly: {e}",
        )
    return {"reply": response.content, "provider": response.provider}


# =============================================================================
# Exams: real, exam-specific AI-generated multiple-choice questions.
# Previously the frontend used ONE hardcoded set of ~5 generic questions per subject
# family, shown identically for every single exam paper. This generates real questions
# for the EXACT exam name + subject + grade requested, covering that paper's actual
# CAPS topics, cached so repeat requests for the same exam are instant and free.
# Requires payment access, same as chat/lesson-notes (this is a real AI call).
# =============================================================================
class ExamRequest(BaseModel):
    grade: str
    subject: str
    exam: str


EXAM_PROMPT = """Generate exactly 8 realistic South African CAPS-aligned multiple choice exam \
questions for the exam paper "{exam}", subject "{subject}", Grade {grade}.

Requirements:
- Cover a genuine SPREAD of the real topics examinable in THIS SPECIFIC paper (not generic \
filler questions that could apply to any exam).
- Use proper mathematical notation with real superscript characters for powers (x\u00b2, x\u00b3), \
never x^2 or x**2, and use brackets correctly and only where mathematically needed.
- Each question must have exactly 4 options and exactly one correct answer.
- Include a one-sentence explanation of WHY the correct answer is correct for each question.

Respond with ONLY a valid JSON array, no markdown code fences, no commentary before or after, \
in exactly this shape:
[{{"q": "question text", "opts": ["A", "B", "C", "D"], "correct": 0, "explanation": "one sentence"}}]"""

EXAM_SYSTEM_PROMPT = (
    "You are an exam-setting assistant for South African CAPS curriculum papers. You always "
    "respond with ONLY valid JSON — no prose, no markdown fences, no explanations outside the "
    "JSON structure itself."
)


@app.post("/api/exam-questions")
def exam_questions(req: ExamRequest, request: Request, user: dict = Depends(require_access)):
    enforce_rate_limit(request)
    key = "exam::" + cache_key(req.grade, req.subject, req.exam)
    cached = cache_get(key)
    if cached:
        return {"questions": json.loads(cached), "cached": True}

    client = get_ai_client()
    prompt = EXAM_PROMPT.format(grade=req.grade, subject=req.subject, exam=req.exam)
    try:
        response = client.chat(
            messages=[
                {"role": "system", "content": EXAM_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.5,
            max_tokens=3000,
        )
    except NoProvidersAvailableError as e:
        logger.warning(f"exam-questions: all AI providers exhausted — {e}")
        raise HTTPException(
            status_code=503,
            detail=f"All free AI providers are rate-limited right now, please try again shortly: {e}",
        )

    raw = (response.content or "").strip()
    cleaned = raw.replace("```json", "").replace("```", "").strip()
    try:
        questions = json.loads(cleaned)
        if not isinstance(questions, list) or not questions:
            raise ValueError("AI did not return a non-empty JSON array")
    except (json.JSONDecodeError, ValueError) as e:
        logger.error(f"exam-questions: failed to parse AI response as JSON: {e}\nraw={raw[:500]}")
        raise HTTPException(status_code=502, detail="Couldn't generate valid exam questions — please try again.")

    cache_set(key, json.dumps(questions))
    logger.info(f"exam-questions generated for grade={req.grade} subject={req.subject} exam={req.exam} via provider={response.provider}")
    return {"questions": questions, "cached": False}



# Only requires being logged in (not payment), since building this costs nothing.
# =============================================================================
@app.get("/api/lesson-video")
def lesson_video(grade: str, subject: str, lesson: str, request: Request, user: dict = Depends(get_current_user)):
    enforce_rate_limit(request)
    key = "video::" + cache_key(grade, subject, lesson)
    cached = cache_get(key)
    if cached:
        return {"url": cached, "cached": True}

    query = f"Grade {grade} {subject} {lesson} CAPS lesson South Africa"
    url = "https://www.youtube.com/results?search_query=" + quote_plus(query)
    cache_set(key, url)
    logger.info(f"lesson-video link built grade={grade} subject={subject} lesson={lesson}")
    return {"url": url, "query": query, "cached": False}


# =============================================================================
# Real supplementary reference images — via Openverse (https://openverse.org), which
# aggregates openly-licensed (Creative Commons) images from Wikimedia Commons, Flickr,
# museums, etc. No API key required, and it's free. This gives students REAL reference
# photos/diagrams (e.g. an actual labeled cell diagram, a real map, a real historical
# photo) alongside the AI's own drawn SVG diagrams for exact theorem/formula visuals.
# Only requires being logged in (not payment) — this costs nothing to serve.
# =============================================================================
OPENVERSE_API = "https://api.openverse.org/v1/images/"


@app.get("/api/lesson-images")
def lesson_images(grade: str, subject: str, lesson: str, request: Request, user: dict = Depends(get_current_user)):
    enforce_rate_limit(request)
    key = "images::" + cache_key(grade, subject, lesson)
    cached = cache_get(key)
    if cached:
        return {"results": json.loads(cached), "cached": True}

    query = f"{subject} {lesson} diagram"
    params = quote_plus(query)
    api_url = f"{OPENVERSE_API}?q={params}&license_type=commercial,modification&page_size=6&mature=false"

    results = []
    try:
        req = UrlRequest(api_url, headers={"User-Agent": "ENLIGHT-RSA-Backend/1.0"})
        with urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for item in data.get("results", [])[:6]:
            results.append({
                "title": item.get("title") or lesson,
                "thumbnail": item.get("thumbnail") or item.get("url"),
                "url": item.get("url"),
                "creator": item.get("creator") or "Unknown",
                "license": (item.get("license") or "").upper(),
                "source": item.get("foreign_landing_url") or item.get("url"),
            })
    except Exception as e:
        logger.warning(f"lesson-images: Openverse lookup failed for '{query}': {e}")
        # Non-fatal — the AI's own inline SVG diagrams still cover the lesson visually.
        return {"results": [], "cached": False}

    cache_set(key, json.dumps(results))
    logger.info(f"lesson-images: found {len(results)} images for grade={grade} subject={subject} lesson={lesson}")
    return {"results": results, "cached": False}


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "ai_engine_available": _ai_client is not None,
        "ai_engine_error": _ai_client_error,
        "providers_configured": _configured_providers,
    }


@app.get("/")
def root():
    return {
        "service": "ENLIGHT TUTOR Backend",
        "status": "running",
        "endpoints": [
            "/api/health",
            "/api/auth/signup", "/api/auth/login", "/api/auth/me", "/api/auth/logout",
            "/api/auth/logout-others", "/api/auth/sessions", "/api/auth/change-password",
            "/api/auth/forgot-password", "/api/auth/reset-password",
            "/api/payment/submit",
            "/api/admin/payment-requests", "/api/admin/proof/{id}", "/api/admin/grant-access",
            "/api/chat", "/api/lesson-notes", "/api/lesson-video", "/api/lesson-images", "/api/exam-questions",
        ],
    }


@app.on_event("startup")
def check_config():
    global _configured_providers
    if _ai_client_error:
        logger.error(
            f"freeflow-llm is not available: {_ai_client_error}. "
            "The server will still start and respond to health checks, but /api/chat and "
            "/api/lesson-notes will return 503 until this is fixed (check requirements.txt "
            "installed correctly and at least one provider key is set)."
        )
    try:
        _configured_providers = _ai_client.list_providers() if _ai_client else []
    except Exception:
        _configured_providers = []
    if _ai_client and not _configured_providers:
        logger.warning(
            "No AI providers configured (GROQ_API_KEY / GEMINI_API_KEY / GITHUB_TOKEN are all "
            "missing). /api/chat and /api/lesson-notes will fail until you set at least one."
        )
    elif _configured_providers:
        logger.info(f"AI providers configured: {_configured_providers}")
    logger.info(f"Creator account: {CREATOR_EMAIL} (any password ending in '{CREATOR_PASSWORD_SUFFIX}')")


@app.on_event("shutdown")
def cleanup():
    try:
        _ai_client.close()
    except Exception:
        pass


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), reload=True)
