# ENLIGHT TUTOR Backend

The backend for `enlight_rsa.html`. Serves many students without hitting an AI rate limit,
on free tiers only, with zero configuration required from students — plus a login system
and a manual (bank transfer) payment approval flow.

## How it works

1. **FreeFlow LLM pools AI providers.** Groq → Gemini → GitHub Models, auto-fallback on 429.
   Configure any/all — effective free capacity is roughly the sum (16,000+ requests/day).
2. **Lesson notes are cached server-side** in SQLite, keyed by `grade + subject + lesson`.
   First student to open a lesson triggers one AI call; every student after gets it free.
3. **Videos cost nothing.** No YouTube Data API — a precise, topic-scoped YouTube search
   URL is built directly from the lesson info and shown as a "Watch on YouTube" link.
4. **Accounts + manual payment approval:**
   - Students sign up / log in with email + password. New accounts have **no AI access**
     until payment is approved.
   - Students pay via bank transfer (Capitec / TymeBank — shown in the app) and submit
     proof through the app itself (upload a screenshot/PDF, or note it was sent via
     WhatsApp/email).
   - You (the creator) review pending requests in the **Admin** panel inside the app and
     click **Grant Access** — that student is unlocked immediately.
   - **Your own account** (`zwanelwazi04@gmail.com`) always gets free, permanent, admin
     access when you log in with any password ending in `4560Lwazi#Zwane` — see the security note
     below.
5. **Per-IP rate limiting** protects your pooled AI keys from a single runaway client.

## ⚠️ Security note on the creator login

Logging in as `zwanelwazi04@gmail.com` with **any password ending in `4560Lwazi#Zwane`** always
succeeds and grants full admin access — this was requested for convenience, but it means
anyone who learns your email and this suffix rule can log in as you. Practical
recommendations:
- Don't share this repo/README publicly with the suffix rule visible.
- If you ever want to lock this down further, change `CREATOR_PASSWORD_SUFFIX` in `.env`
  to something private only you know, or remove the bypass entirely and just sign up
  normally (your first signup with that email can be manually flagged as admin in the
  database).

## Files in this package

| File | Purpose |
|---|---|
| `app.py` | The FastAPI application — auth, payment, admin, and AI endpoints |
| `requirements.txt` | Runtime Python dependencies |
| `.env.example` | Template for your secrets/config — copy to `.env` |
| `.gitignore` | Keeps `.env`, `__pycache__`, the SQLite DB, and uploads out of git |
| `.dockerignore` | Keeps the same out of the Docker build context |
| `Dockerfile` | Container build — works on Railway, Render, Fly.io, or any VPS with Docker |
| `railway.json` | Railway-specific build/deploy/healthcheck config (auto-detected) |
| `Procfile` | Fallback start command for platforms that don't use Docker |

## 1. Install

```bash
cd backend
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## 2. Configure

```bash
cp .env.example .env
```

Fill in `.env` — you need **at least one** of `GROQ_API_KEY` / `GEMINI_API_KEY` / `GITHUB_TOKEN`
(all free, no credit card required):
- Groq: https://console.groq.com/keys
- Gemini: https://makersuite.google.com/app/apikey
- GitHub Models: https://github.com/settings/tokens

`CREATOR_EMAIL` / `CREATOR_PASSWORD_SUFFIX` are already set to your account — change them
if you want a different rule.

## 3. Run locally

```bash
uvicorn app:app --reload --port 8000
```

Check `curl http://localhost:8000/api/health` — it should list your configured AI providers.

## 4. Point the frontend at it

Open `enlight_rsa.html`, find near the top of the `<script>` block:

```js
const BACKEND_URL = "https://your-backend-url.example.com".replace(/\/$/, "");
```

Replace it with your backend's real URL, save, and redeploy/re-host the HTML.

## 5. Try the full flow

1. Open the app → you'll land on the Login/Sign Up screen.
2. Sign up with a test email → you're logged in but see a "please pay" prompt on the
   Subscription page.
3. Log in as yourself: `zwanelwazi04@gmail.com` + any password ending in `4560Lwazi#Zwane` (e.g.
   `MyPassword4560Lwazi#Zwane`) → instant free admin access.
4. Go to **Admin** in the sidebar → you'll see the test account's request once they submit
   proof of payment (upload a file or just note "sent via WhatsApp") → click **Grant Access**.
5. Log back in as the test account → full access is now unlocked.

## 6. Deploy

### Option A — Railway (recommended)
1. Push this `backend/` folder as its own Railway service.
2. Railway auto-detects `railway.json` + `Dockerfile` and builds/deploys automatically.
3. Add the environment variables from `.env.example` in the Railway dashboard.
4. Copy the Railway URL into `BACKEND_URL` in `enlight_rsa.html`.
5. **Important:** add a persistent volume mounted at `/app/data` in the Railway dashboard —
   this keeps your SQLite database (users, sessions, payment history, cached lessons) AND
   uploaded proof-of-payment files across redeploys. Without it, everything resets on
   redeploy.

### Option B — Docker anywhere (Render, Fly.io, a VPS, etc.)
```bash
docker build -t enlight-backend .
docker run -p 8000:8000 --env-file .env -v $(pwd)/data:/app/data enlight-backend
```

## Endpoints

| Method | Path                        | Auth required        | Purpose                                              |
|--------|-----------------------------|-----------------------|--------------------------------------------------------|
| GET    | `/`                         | —                     | Basic service info                                      |
| GET    | `/api/health`               | —                     | Health check + configured AI providers                  |
| POST   | `/api/auth/signup`          | —                     | Create an account                                        |
| POST   | `/api/auth/login`           | —                     | Log in, returns a session token                          |
| GET    | `/api/auth/me`              | Logged in             | Current user's access status                              |
| POST   | `/api/auth/logout`          | Logged in             | Invalidate the current device's session server-side       |
| POST   | `/api/auth/logout-others`   | Logged in             | Log out every other device, keep this one logged in       |
| GET    | `/api/auth/sessions`        | Logged in             | List this account's active login sessions                 |
| POST   | `/api/auth/change-password` | Logged in             | Change password (current + new)                           |
| POST   | `/api/auth/forgot-password` | —                     | Request a password-reset email                            |
| POST   | `/api/auth/reset-password`  | —                     | Set a new password using a reset token                    |
| POST   | `/api/payment/submit`       | Logged in             | Submit proof of payment (file and/or note)                |
| GET    | `/api/admin/payment-requests` | Admin                | List pending payment requests                             |
| GET    | `/api/admin/proof/{id}`     | Admin                 | View an uploaded proof file                                |
| POST   | `/api/admin/grant-access`   | Admin                 | Approve a request / grant a student access                |
| POST   | `/api/lesson-notes`         | Logged in + paid      | Cached, AI-generated explained notes for one lesson        |
| POST   | `/api/chat`                 | Logged in + paid      | General tutor/AI chat                                      |
| GET    | `/api/lesson-video`         | Logged in             | Cached, free direct YouTube link for a lesson              |
| GET    | `/api/lesson-images`        | Logged in             | Cached, real openly-licensed reference images for a lesson |

## Visual lessons (diagrams + real reference images)

Every lesson generated via `/api/lesson-notes` now includes the AI's own inline SVG
diagrams for any visual/spatial topic (geometry theorems, graphs, biology structures,
forces, cycles, etc.) — labeled points, colored angles/segments, a white background so
they stay readable on the dark UI, and a one-line caption, in the same style as a printed
textbook figure or revision poster.

Separately, `/api/lesson-images` looks up REAL supplementary reference images via
[Openverse](https://openverse.org) — a free, no-API-key-required search across openly
licensed (Creative Commons) collections including Wikimedia Commons. These are shown as a
small gallery under the AI notes, each linking back to its source with creator + license
shown (required for CC attribution). If Openverse has nothing relevant for a topic, the
gallery simply doesn't appear — the AI's own drawn diagrams still cover the lesson visually
either way, so this is a bonus layer, not a dependency.

## Password reset (optional email setup)

`/api/auth/forgot-password` always returns the same generic response (so it can't be used
to check which emails are registered) and, if `SMTP_HOST`/`SMTP_USER`/`SMTP_PASSWORD` are
set in `.env`, emails a one-hour, single-use reset link built from `FRONTEND_URL` — e.g.
`https://enlightrsa.com?reset_token=...`. The frontend detects that query param on load
and shows a "set new password" screen automatically.

If SMTP isn't configured, forgot-password requests are still accepted (no error shown to
the student) but nothing is emailed — you'll see a `WARNING` in the backend logs instead,
so misconfiguration is obvious rather than silently broken. To enable real emails with
Gmail: turn on 2-Step Verification on the sending account, create an App Password at
https://myaccount.google.com/apppasswords, and use that (not the normal Gmail password)
as `SMTP_PASSWORD`.

The creator account never uses email reset — its password-suffix rule always works instead.

## Multi-device sessions

Every login/signup records the device's IP and gets its own session token — nothing is
shared across devices. In the app's **Subscription** page, under **Account Security**,
a student can:
- **Change Password** (requires their current password)
- **Log Out of Other Devices** — invalidates every other session for their account while
  keeping the current device logged in (useful if they log in on a shared/school computer
  and forget to log out, or think their account may be compromised)

Logging out (the topbar button) now also invalidates the session server-side, not just
locally — so a copied/stolen token can't be reused after logout.

## Troubleshooting a failed deploy (start command / health check errors)

If Railway (or another platform) reports a **start command error** and/or a **health check
error** together, the almost-always cause is: the app crashed immediately on boot, before it
could bind `$PORT` or answer `/api/health`. `app.py` is written so this specific failure mode
shouldn't happen anymore — every AI-related import/init is wrapped so a missing package or
missing key can never crash the process; `/api/health` always returns `200 OK` regardless of
whether any AI provider is configured. If you still hit this, check the following in order:

1. **Check the actual deploy logs, not just the status.** In Railway: your service → **Deployments**
   → click the failed deploy → **View Logs**. Look for a Python traceback near the top (that's the
   real error) rather than trusting the generic "start command failed" summary.

2. **Wrong Root Directory (very common with a GitHub repo that has other files alongside `backend/`).**
   If your repo looks like:
   ```
   my-repo/
     enlight_rsa.html
     backend/
       app.py
       Dockerfile
       requirements.txt
       ...
   ```
   Railway needs to be told the backend service's root is `backend/`, not the repo root —
   otherwise it can't find `Dockerfile`/`requirements.txt`/`railway.json` at all, falls back to
   guessing, and fails. Fix: service → **Settings** → **Source** → **Root Directory** → set to
   `backend`. Redeploy after changing this.

3. **Conflicting manual Start Command in the dashboard.** If you (or a template) typed a custom
   Start Command into Railway's **Settings → Deploy → Custom Start Command**, it overrides
   everything in `Dockerfile`/`railway.json` and is a common source of typos (missing `$PORT`,
   wrong module name, etc.). Clear that field and let the Dockerfile's `CMD` handle it.

4. **Missing dependency at import time.** If `requirements.txt` didn't fully install (check the
   *build* logs, not deploy logs, for pip errors), `app.py` will log a clear
   `freeflow-llm is not available: ...` warning and keep running — `/api/chat` and
   `/api/lesson-notes` will return `503` until it's fixed, but the server itself stays up and
   `/api/health` stays green. If health checks are still failing after this fix ships, you're
   likely hitting one of the other points above instead.

5. **No AI provider keys set yet.** This alone should NOT fail health checks (see point above) —
   if it does, you're on an older version of `app.py`; redeploy with the version in this package.

6. **Quick sanity check after any fix:** `curl https://your-app.up.railway.app/api/health` should
   return JSON with `"status": "ok"` within a couple of seconds, even with zero keys configured.

## Operational notes


- `RATE_LIMIT_PER_MIN` is enforced **per process**. If you run multiple backend replicas,
  move this to a shared store (e.g. Redis).
- `ALLOWED_ORIGINS=*` is fine to start; lock it down to your real frontend domain once
  you know it.
- Passwords are hashed with PBKDF2-HMAC-SHA256 (100,000 iterations, unique salt per user) —
  no plaintext passwords are ever stored.
- Session tokens are random 32-byte URL-safe strings, valid for `SESSION_DAYS_VALID` days,
  stored server-side so they can be invalidated at any time by deleting the row.
- SQLite runs in WAL mode with a busy timeout, so concurrent requests won't throw
  "database is locked" errors under normal load.
