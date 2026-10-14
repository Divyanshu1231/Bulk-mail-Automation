import asyncio
import base64
import binascii
import csv
import io
import logging
import os
import random
import sqlite3
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field

load_dotenv()

DB_PATH = os.getenv("DATABASE_PATH", "./email_automation.db")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest").strip()
BREVO_API_KEY = os.getenv("BREVO_API_KEY", "").strip()
BREVO_SENDER_EMAIL = os.getenv("BREVO_SENDER_EMAIL", "").strip()
BREVO_SENDER_NAME = os.getenv("BREVO_SENDER_NAME", "Recruitment Team").strip()
FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "https://bulk-mail-frontend.onrender.com").strip().rstrip("/")
_DEFAULT_CORS = f"{FRONTEND_ORIGIN},http://localhost:5173,http://127.0.0.1:5173"
CORS_ORIGINS = [origin.strip().rstrip("/") for origin in os.getenv("CORS_ORIGINS", _DEFAULT_CORS).split(",") if origin.strip()]
CORS_ORIGIN_REGEX = os.getenv("CORS_ORIGIN_REGEX", "").strip() or None  # e.g. https://.*--your-site\\.netlify\\.app
MAX_SEND_PER_REQUEST = max(1, min(int(os.getenv("MAX_SEND_PER_REQUEST", "20")), 100))

GEMINI_FALLBACK_MODELS = [m.strip() for m in os.getenv("GEMINI_FALLBACK_MODELS", "").split(",") if m.strip() and m.strip() != GEMINI_MODEL]
GEMINI_MAX_RETRIES = max(0, min(int(os.getenv("GEMINI_MAX_RETRIES", "2")), 5))          # retries after the first attempt
GEMINI_TOTAL_TIMEOUT = float(os.getenv("GEMINI_TOTAL_TIMEOUT_SECONDS", "60"))            # hard budget per user request
GEMINI_REQUEST_TIMEOUT = float(os.getenv("GEMINI_REQUEST_TIMEOUT_SECONDS", "20"))        # per HTTP attempt
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
STALE_SENDING_MINUTES = 10

logger = logging.getLogger("email_automation")
logging.getLogger("httpx").setLevel(logging.WARNING)      # httpx INFO logs print full URLs
logging.getLogger("httpcore").setLevel(logging.WARNING)

_gemini_transport = None   # tests inject an httpx.MockTransport here
_sleep = asyncio.sleep     # tests replace this to avoid real waiting

MAX_ATTACHMENTS = 5
MAX_ATTACHMENT_TOTAL_BYTES = int(os.getenv("MAX_ATTACHMENT_TOTAL_MB", "3")) * 1024 * 1024  # Brevo limit is ~4 MB per email; keep a safety margin
ALLOWED_ATTACHMENT_EXT = {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "csv", "png", "jpg", "jpeg", "gif", "webp"}

app = FastAPI(title="AI Email Automation API", version="1.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_origin_regex=CORS_ORIGIN_REGEX,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

def db():
    db_dir = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(db_dir, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS contacts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          email TEXT NOT NULL UNIQUE,
          name TEXT DEFAULT '',
          company TEXT DEFAULT '',
          role TEXT DEFAULT '',
          context TEXT DEFAULT '',
          consent INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL
        )""")
        con.execute("""
        CREATE TABLE IF NOT EXISTS drafts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          contact_id INTEGER NOT NULL,
          subject TEXT NOT NULL,
          body TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'draft',
          provider_message_id TEXT DEFAULT '',
          error TEXT DEFAULT '',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          FOREIGN KEY(contact_id) REFERENCES contacts(id)
        )""")
        con.execute("""
        CREATE TABLE IF NOT EXISTS campaigns (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          subject_template TEXT NOT NULL,
          body_template TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'queued',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )""")
        con.execute("""
        CREATE TABLE IF NOT EXISTS campaign_recipients (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          campaign_id INTEGER NOT NULL,
          contact_id INTEGER NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending',
          provider_message_id TEXT DEFAULT '',
          error TEXT DEFAULT '',
          updated_at TEXT NOT NULL,
          UNIQUE(campaign_id, contact_id),
          FOREIGN KEY(campaign_id) REFERENCES campaigns(id),
          FOREIGN KEY(contact_id) REFERENCES contacts(id)
        )""")

def migrate_db():
    with db() as con:
        cols = [r["name"] for r in con.execute("PRAGMA table_info(campaigns)").fetchall()]
        if "attachments_json" not in cols:
            con.execute("ALTER TABLE campaigns ADD COLUMN attachments_json TEXT NOT NULL DEFAULT '[]'")
        if "request_id" not in cols:
            con.execute("ALTER TABLE campaigns ADD COLUMN request_id TEXT")
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_campaigns_request_id ON campaigns(request_id) WHERE request_id IS NOT NULL")

init_db()
migrate_db()

def now():
    return datetime.now(timezone.utc).isoformat()

def normalize_consent(value: str) -> bool:
    return (value or "").strip().lower() in {"yes", "true", "1", "y", "consented"}

def safe_email(value: str) -> bool:
    value = (value or "").strip()
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value))

def contact_dict(row):
    d = dict(row)
    d["consent"] = bool(d.get("consent"))
    return d

def draft_dict(row):
    return dict(row)

class GenerateRequest(BaseModel):
    contact_ids: list[int] = Field(default_factory=list, max_length=200)
    instruction: str = Field(default="Write a concise, professional recruitment partnership email. Do not invent facts, claims, or promises.", max_length=2000)

class EditDraftRequest(BaseModel):
    subject: str = Field(min_length=1, max_length=250)
    body: str = Field(min_length=1, max_length=12000)

class ApproveRequest(BaseModel):
    approved: bool

class SendRequest(BaseModel):
    draft_ids: list[int] = Field(min_length=1, max_length=100)

class TemplateGenerateRequest(BaseModel):
    instruction: str = Field(default="Write a concise, professional recruitment outreach email. Do not invent facts, claims, or promises. Use {{name}}, {{company}}, and {{role}} placeholders where suitable.", max_length=2000)

class AttachmentIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1)  # base64 (no data: prefix)

class CampaignCreateRequest(BaseModel):
    subject: str = Field(min_length=1, max_length=250)
    body: str = Field(min_length=1, max_length=12000)
    attachments: list[AttachmentIn] = Field(default_factory=list, max_length=MAX_ATTACHMENTS)
    request_id: Optional[str] = Field(default=None, max_length=64)  # idempotency key: same id never creates a second campaign

def validate_attachments(items) -> list[dict]:
    """Validate type/size and return clean [{name, content}] for Brevo."""
    clean, total = [], 0
    for item in items:
        name = re.sub(r"[^A-Za-z0-9._ \-()]", "_", os.path.basename(item.name.strip())) or "attachment"
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in ALLOWED_ATTACHMENT_EXT:
            raise HTTPException(400, f"File type .{ext or '?'} is not allowed. Allowed: {', '.join(sorted(ALLOWED_ATTACHMENT_EXT))}.")
        content = item.content.split(",", 1)[-1].strip()  # tolerate data: URLs
        try:
            raw = base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(400, f"Attachment '{name}' is not valid base64.")
        if not raw:
            raise HTTPException(400, f"Attachment '{name}' is empty.")
        total += len(raw)
        if total > MAX_ATTACHMENT_TOTAL_BYTES:
            raise HTTPException(400, f"Attachments are too large. Keep the total under {MAX_ATTACHMENT_TOTAL_BYTES // (1024 * 1024)} MB.")
        clean.append({"name": name, "content": content})
    return clean

class CampaignProcessRequest(BaseModel):
    limit: int = Field(default=20, ge=1, le=100)

@app.get("/")
def root():
    return {"ok": True, "service": "AI Email Automation API", "health": "/api/health"}

@app.get("/api/health")
def health():
    return {"ok": True, "gemini_configured": bool(GEMINI_API_KEY),
            "brevo_configured": bool(BREVO_API_KEY and BREVO_SENDER_EMAIL),
            "max_send_per_request": MAX_SEND_PER_REQUEST, "gemini_model": GEMINI_MODEL,
            "max_attachments": MAX_ATTACHMENTS, "max_attachment_mb": MAX_ATTACHMENT_TOTAL_BYTES // (1024 * 1024)}

@app.get("/api/contacts")
def list_contacts():
    with db() as con:
        rows = con.execute("SELECT * FROM contacts ORDER BY id DESC").fetchall()
    return [contact_dict(r) for r in rows]

@app.post("/api/contacts/{contact_id}/consent")
def set_contact_consent(contact_id: int, payload: ApproveRequest):
    # User must explicitly confirm they are authorized to email this contact.
    with db() as con:
        row = con.execute("SELECT id FROM contacts WHERE id=?", (contact_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Contact not found.")
        con.execute("UPDATE contacts SET consent=? WHERE id=?", (int(payload.approved), contact_id))
    return {"ok": True, "consent": payload.approved}


@app.post("/api/contacts/upload")
async def upload_contacts(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".csv"):
        raise HTTPException(400, "Please upload a .csv file.")
    raw = await file.read()
    if len(raw) > 5_000_000:
        raise HTTPException(413, "CSV must be smaller than 5 MB.")
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(400, "Save the CSV as UTF-8 and try again.")
    reader = csv.DictReader(io.StringIO(content))
    # Accept common header variants such as Email, E-mail, email address.
    def norm_header(value):
        key = re.sub(r"[^a-z0-9]+", "", (value or "").strip().lower())
        aliases = {
            "email": "email", "emailaddress": "email", "e-mail": "email",
            "name": "name", "fullname": "name", "company": "company",
            "organization": "company", "role": "role", "jobtitle": "role",
            "context": "context", "notes": "context", "consent": "consent",
            "permission": "consent", "authorized": "consent"
        }
        return aliases.get(key, key)

    if not reader.fieldnames:
        raise HTTPException(400, "CSV header row is missing.")
    normalized_headers = [norm_header(h) for h in reader.fieldnames]
    if "email" not in normalized_headers:
        raise HTTPException(400, "CSV must contain an Email column (e.g. Email or email).")
    added, updated, skipped = 0, 0, 0
    with db() as con:
        for source in reader:
            row = {}
            for original_key, value in source.items():
                key = norm_header(original_key)
                row[key] = (value or "").strip()
            email = row.get("email", "").lower()
            if not safe_email(email):
                skipped += 1
                continue
            values = (
                email, row.get("name", "")[:200], row.get("company", "")[:200],
                row.get("role", "")[:200], row.get("context", "")[:2000],
                int(normalize_consent(row.get("consent", ""))), now()
            )
            existing = con.execute(
                "SELECT id FROM contacts WHERE email = ?", (email,)
            ).fetchone()
            con.execute("""INSERT INTO contacts(email,name,company,role,context,consent,created_at)
                           VALUES(?,?,?,?,?,?,?)
                           ON CONFLICT(email) DO UPDATE SET
                             name=excluded.name,
                             company=excluded.company,
                             role=excluded.role,
                             context=excluded.context,
                             consent=excluded.consent""", values)
            if existing:
                updated += 1
            else:
                added += 1
    return {
        "added": added,
        "updated": updated,
        "skipped": skipped,
        "message": "Contacts imported. Consent was read from the CSV; only use yes when valid permission exists."
    }

class GeminiError(Exception):
    """Gemini failure with a stable code the frontend can act on."""
    def __init__(self, status_code, code, message, retryable=False, retry_after=None, transient=False):
        super().__init__(message)
        self.status_code, self.code, self.message = status_code, code, message
        self.retryable, self.retry_after, self.transient = retryable, retry_after, transient

@app.exception_handler(GeminiError)
async def gemini_error_handler(request, exc: GeminiError):
    headers = {"Retry-After": str(int(exc.retry_after))} if exc.retry_after else None
    return JSONResponse(status_code=exc.status_code, headers=headers, content={
        "detail": exc.message, "code": exc.code, "retryable": exc.retryable, "retry_after": exc.retry_after})

def _safe(text: str, limit: int = 300) -> str:
    text = (text or "")[:limit].replace("\n", " ")
    return text.replace(GEMINI_API_KEY, "***") if GEMINI_API_KEY else text

def _retry_after_seconds(response) -> Optional[float]:
    raw = response.headers.get("retry-after")
    try:
        return min(max(float(raw), 0.0), 20.0) if raw else None
    except ValueError:
        return None

def _classify_gemini_response(response) -> GeminiError:
    status, body = response.status_code, response.text or ""
    retry_after = _retry_after_seconds(response)
    if status == 429:
        return GeminiError(429, "gemini_rate_limited", "Gemini rate limit or quota reached. Wait a minute and retry, or use a manual template.", True, retry_after or 30, True)
    if status in (500, 502, 503, 504):
        return GeminiError(503, "gemini_unavailable", "Gemini is temporarily busy (high demand). Retry in a moment, or use a manual template.", True, retry_after, True)
    if status in (401, 403) or "API_KEY_INVALID" in body or "API key not valid" in body:
        return GeminiError(502, "gemini_auth_error", "Gemini rejected the API key or permission. Check GEMINI_API_KEY in the backend environment.", False)
    if status == 404:
        return GeminiError(502, "gemini_model_not_found", f"Gemini model '{GEMINI_MODEL}' was not found or does not support generateContent. Check GEMINI_MODEL.", False)
    return GeminiError(502, "gemini_bad_request", f"Gemini rejected the request (HTTP {status}). Shorten the instruction or check GEMINI_MODEL.", False)

async def _gemini_attempts(model: str, prompt: str, deadline: float, max_retries: int) -> str:
    loop = asyncio.get_running_loop()
    url = f"{GEMINI_BASE_URL}/models/{model}:generateContent"
    payload = {"contents": [{"parts": [{"text": prompt}]}],
               "generationConfig": {"temperature": 0.4, "responseMimeType": "application/json"}}
    attempt = 0
    while True:
        remaining = deadline - loop.time()
        if remaining < 1:
            raise GeminiError(504, "gemini_timeout", "Gemini took too long to respond. Please retry, or use a manual template.", True, None, True)
        try:
            async with httpx.AsyncClient(timeout=min(GEMINI_REQUEST_TIMEOUT, remaining), transport=_gemini_transport) as client:
                # API key goes in a header (never in the URL) so it cannot leak into logs.
                response = await client.post(url, json=payload, headers={"x-goog-api-key": GEMINI_API_KEY})
        except httpx.TimeoutException:
            err = GeminiError(504, "gemini_timeout", "Gemini took too long to respond. Please retry, or use a manual template.", True, None, True)
        except httpx.RequestError as exc:
            err = GeminiError(503, "gemini_unreachable", "Could not connect to Gemini. Please retry, or use a manual template.", True, None, True)
            logger.warning("Gemini connection error model=%s type=%s", model, type(exc).__name__)
        else:
            if response.status_code < 400:
                try:
                    return response.json()["candidates"][0]["content"]["parts"][0]["text"]
                except (KeyError, IndexError, TypeError, ValueError):
                    err = GeminiError(502, "gemini_bad_response", "Gemini returned an unexpected response. Please retry.", True, None, True)
            else:
                err = _classify_gemini_response(response)
                logger.warning("Gemini API failed: model=%s attempt=%d status=%s body=%s", model, attempt + 1, response.status_code, _safe(response.text))
        if not err.transient or attempt >= max_retries:
            raise err
        delay = err.retry_after if err.retry_after else min(1.0 * (2 ** attempt), 8.0) + random.uniform(0, 0.5)
        if delay >= deadline - loop.time() - 1:
            raise err   # not enough time budget left to wait and retry
        logger.info("Gemini retry in %.1fs (model=%s attempt=%d code=%s)", delay, model, attempt + 1, err.code)
        await _sleep(delay)
        attempt += 1

async def call_gemini(prompt: str) -> tuple[str, str]:
    """Returns (text, model_used). Bounded retries; optional configured fallback models."""
    if not GEMINI_API_KEY:
        raise GeminiError(400, "gemini_not_configured", "GEMINI_API_KEY is not configured. You can still use a manual template.", False)
    loop = asyncio.get_running_loop()
    start = loop.time()
    overall_deadline = start + GEMINI_TOTAL_TIMEOUT
    models = [GEMINI_MODEL] + GEMINI_FALLBACK_MODELS
    last = None
    for index, model in enumerate(models):
        # With fallbacks configured, the primary model may only use ~55% of the time budget,
        # so a configured fallback model always gets a real chance (503 responses can be slow).
        if index == 0 and len(models) > 1:
            deadline = start + GEMINI_TOTAL_TIMEOUT * 0.55
        else:
            deadline = overall_deadline
        try:
            text = await _gemini_attempts(model, prompt, deadline, GEMINI_MAX_RETRIES if index == 0 else 1)
            if index:
                logger.warning("Gemini fallback model used: %s (primary %s was unavailable)", model, GEMINI_MODEL)
            else:
                logger.info("Gemini model used: %s", model)
            return text, model
        except GeminiError as exc:
            last = exc
            if not exc.transient:
                if index == 0 and exc.code in ("gemini_model_not_found",) and len(models) > 1:
                    logger.warning("Primary Gemini model %s not usable (%s); trying configured fallback", model, exc.code)
                    continue
                raise
            if overall_deadline - loop.time() < 1:
                break
    raise last

def parse_subject_body(text: str) -> tuple[str, str]:
    cleaned = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.MULTILINE).strip()
    try:
        data = json.loads(cleaned)
        subject, body = str(data["subject"]).strip(), str(data["body"]).strip()
        if not subject or not body:
            raise ValueError("empty")
        return subject[:250], body[:12000]
    except Exception:
        raise GeminiError(502, "gemini_bad_response", "Could not parse Gemini's response. Please retry, or use a manual template.", True)

async def generate_with_gemini(contact: dict, instruction: str) -> tuple[str, str]:
    prompt = ("Create one email draft using the data below.\n"
              'Return ONLY valid JSON with keys "subject" and "body". No markdown fences.\n'
              "Do not invent facts. Do not imply the recipient has agreed to anything.\n"
              "Keep it concise and professional. Do not include deceptive urgency.\n"
              f"Instruction: {instruction}\nRecipient name: {contact.get('name','')}\nCompany: {contact.get('company','')}\n"
              f"Role: {contact.get('role','')}\nContext supplied by sender: {contact.get('context','')}\nSender display name: {BREVO_SENDER_NAME}\n")
    text, _model = await call_gemini(prompt)
    return parse_subject_body(text)

@app.get("/api/ai/check")
async def ai_check():
    """Verify the configured Gemini model exists and supports generateContent (does not use generation quota)."""
    info = {"configured": bool(GEMINI_API_KEY), "model": GEMINI_MODEL, "fallback_models": GEMINI_FALLBACK_MODELS,
            "supported": None, "detail": "", "fallbacks": []}
    if not GEMINI_API_KEY:
        info["detail"] = "GEMINI_API_KEY is not configured."
        return info
    try:
        async with httpx.AsyncClient(timeout=15, transport=_gemini_transport) as client:
            response = await client.get(f"{GEMINI_BASE_URL}/models/{GEMINI_MODEL}", headers={"x-goog-api-key": GEMINI_API_KEY})
            for fb in GEMINI_FALLBACK_MODELS:
                fb_resp = await client.get(f"{GEMINI_BASE_URL}/models/{fb}", headers={"x-goog-api-key": GEMINI_API_KEY})
                ok = fb_resp.status_code == 200 and "generateContent" in fb_resp.json().get("supportedGenerationMethods", [])
                info["fallbacks"].append({"model": fb, "supported": ok if fb_resp.status_code in (200, 404) else None})
    except httpx.HTTPError:
        info["detail"] = "Could not reach Gemini to verify the model."
        return info
    if response.status_code == 200:
        methods = response.json().get("supportedGenerationMethods", [])
        info["supported"] = "generateContent" in methods
        info["detail"] = "Model is available." if info["supported"] else "Model does not support generateContent."
    elif response.status_code == 404:
        info["supported"], info["detail"] = False, "Model not found. Check GEMINI_MODEL."
    else:
        info["detail"] = f"Could not verify model (HTTP {response.status_code})."
        logger.warning("Gemini model check status=%s body=%s", response.status_code, _safe(response.text))
    return info

@app.post("/api/drafts/generate")
async def generate_drafts(payload: GenerateRequest):
    if not payload.contact_ids:
        raise HTTPException(400, "Select at least one contact.")
    if len(payload.contact_ids) > 50:
        raise HTTPException(400, "Generate at most 50 drafts per request to control free-tier usage.")
    created = []
    with db() as con:
        contacts = []
        for cid in payload.contact_ids:
            row = con.execute("SELECT * FROM contacts WHERE id=?", (cid,)).fetchone()
            if row:
                contacts.append(dict(row))
    if not contacts:
        raise HTTPException(404, "No matching contacts found.")
    for contact in contacts:
        subject, body = await generate_with_gemini(contact, payload.instruction)
        timestamp = now()
        with db() as con:
            cur = con.execute("""INSERT INTO drafts(contact_id,subject,body,status,created_at,updated_at)
                                 VALUES(?,?,?,'draft',?,?)""",
                              (contact["id"], subject, body, timestamp, timestamp))
            did = cur.lastrowid
        created.append({"id": did, "contact_id": contact["id"], "email": contact["email"],
                        "subject": subject, "body": body, "status": "draft"})
    return {"created": created}

@app.get("/api/drafts")
def list_drafts():
    with db() as con:
        rows = con.execute("""SELECT d.*, c.email, c.name, c.company, c.consent
                              FROM drafts d JOIN contacts c ON c.id=d.contact_id
                              ORDER BY d.id DESC""").fetchall()
    return [dict(r) for r in rows]

@app.put("/api/drafts/{draft_id}")
def edit_draft(draft_id: int, payload: EditDraftRequest):
    with db() as con:
        row = con.execute("SELECT status FROM drafts WHERE id=?", (draft_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Draft not found.")
        if row["status"] in {"sent", "sending"}:
            raise HTTPException(409, "Sent/sending drafts cannot be edited.")
        con.execute("UPDATE drafts SET subject=?, body=?, status='draft', error='', updated_at=? WHERE id=?",
                    (payload.subject.strip(), payload.body.strip(), now(), draft_id))
    return {"ok": True, "status": "draft"}

@app.post("/api/drafts/{draft_id}/approve")
def approve_draft(draft_id: int, payload: ApproveRequest):
    with db() as con:
        row = con.execute("""SELECT d.status, c.consent FROM drafts d
                             JOIN contacts c ON c.id=d.contact_id WHERE d.id=?""", (draft_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Draft not found.")
        if row["status"] in {"sent", "sending"}:
            raise HTTPException(409, "Sent/sending drafts cannot be approved again.")
        if payload.approved and not row["consent"]:
            raise HTTPException(400, "This contact is not marked as consented in the CSV. Do not send without valid permission.")
        status = "approved" if payload.approved else "draft"
        con.execute("UPDATE drafts SET status=?, error='', updated_at=? WHERE id=?", (status, now(), draft_id))
    return {"ok": True, "status": status}

async def send_via_brevo(email: str, name: str, subject: str, body: str, attachments: Optional[list] = None):
    if not BREVO_API_KEY or not BREVO_SENDER_EMAIL:
        raise HTTPException(400, "Set BREVO_API_KEY and BREVO_SENDER_EMAIL in backend/.env.")
    payload = {
        "sender": {"name": BREVO_SENDER_NAME, "email": BREVO_SENDER_EMAIL},
        "to": [{"email": email, "name": name or email}],
        "subject": subject,
        "textContent": body + "\n\nIf you no longer wish to receive recruitment-related emails, please reply asking to be removed from our contact list."
    }
    if attachments:
        payload["attachment"] = [{"name": a["name"], "content": a["content"]} for a in attachments]
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={"api-key": BREVO_API_KEY, "accept": "application/json", "content-type": "application/json"},
            json=payload
        )
    if response.status_code >= 400:
        text = response.text[:800]
        if response.status_code == 401 and "unrecognised IP" in text:
            found = re.search(r"IP address ([0-9a-fA-F:.]+)", text)
            ip = found.group(1).rstrip(".") if found else "unknown"
            raise RuntimeError(
                f"Brevo blocked this server's IP address ({ip}). Fix: in Brevo open Security > Authorised IPs "
                f"(https://app.brevo.com/security/authorised_ips) and deactivate IP blocking, because Render's IP changes. "
                f"Brevo said: {text}")
        raise RuntimeError(f"Brevo {response.status_code}: {text}")
    return response.json().get("messageId", "")

def personalize_template(template: str, contact: dict) -> str:
    values = {
        "name": contact.get("name") or "there",
        "email": contact.get("email") or "",
        "company": contact.get("company") or "your organization",
        "role": contact.get("role") or "",
        "context": contact.get("context") or "",
    }
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", str(value))
    return template

@app.post("/api/template/generate")
async def generate_one_template(payload: TemplateGenerateRequest):
    """Generate one reusable template (optional). Campaigns also work with a manually typed template."""
    prompt = ("Create ONE reusable professional recruitment outreach email template.\n"
              "Return ONLY valid JSON with keys subject and body. No markdown fences.\n"
              "Do not invent facts, claims, urgency, or promises. Keep it concise.\n"
              "Use these exact placeholders when personalization is relevant: {{name}}, {{company}}, {{role}}.\n"
              f"Instruction: {payload.instruction}\nSender display name: {BREVO_SENDER_NAME}\n")
    text, model = await call_gemini(prompt)
    subject, body = parse_subject_body(text)
    return {"subject": subject, "body": body, "model": model}

@app.post("/api/campaigns")
def create_campaign(payload: CampaignCreateRequest):
    if not BREVO_API_KEY or not BREVO_SENDER_EMAIL:
        raise HTTPException(400, "Brevo is not configured. Check Render environment variables.")
    if not payload.subject.strip() or not payload.body.strip():
        raise HTTPException(400, "Subject and email body are required.")
    attachments = validate_attachments(payload.attachments)
    timestamp = now()
    request_id = (payload.request_id or "").strip() or None
    with db() as con:
        if request_id:
            existing = con.execute("SELECT id, status, attachments_json FROM campaigns WHERE request_id=?", (request_id,)).fetchone()
            if existing:
                total = con.execute("SELECT COUNT(*) n FROM campaign_recipients WHERE campaign_id=?", (existing["id"],)).fetchone()["n"]
                names = [a["name"] for a in json.loads(existing["attachments_json"] or "[]")]
                return {"campaign_id": existing["id"], "total": total, "status": existing["status"],
                        "batch_size": min(MAX_SEND_PER_REQUEST, 20), "attachments": names, "duplicate": True}
        recipients = con.execute("SELECT id FROM contacts WHERE consent=1 ORDER BY id").fetchall()
        if not recipients:
            raise HTTPException(400, "No authorized contacts found. Import contacts and mark permission only where valid consent exists.")
        cur = con.execute("INSERT INTO campaigns(subject_template,body_template,status,created_at,updated_at,attachments_json,request_id) VALUES(?,?, 'queued', ?,?,?,?)",
                          (payload.subject.strip(), payload.body.strip(), timestamp, timestamp, json.dumps(attachments), request_id))
        campaign_id = cur.lastrowid
        con.executemany("INSERT INTO campaign_recipients(campaign_id,contact_id,status,updated_at) VALUES(?,?,'pending',?)",
                        [(campaign_id, r["id"], timestamp) for r in recipients])
    return {"campaign_id": campaign_id, "total": len(recipients), "status": "queued", "batch_size": min(MAX_SEND_PER_REQUEST, 20), "attachments": [a["name"] for a in attachments]}

@app.get("/api/campaigns/{campaign_id}")
def get_campaign(campaign_id: int):
    with db() as con:
        campaign = con.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if not campaign:
            raise HTTPException(404, "Campaign not found.")
        counts = {r["status"]: r["n"] for r in con.execute(
            "SELECT status, COUNT(*) n FROM campaign_recipients WHERE campaign_id=? GROUP BY status", (campaign_id,)).fetchall()}
        total = sum(counts.values())
    return {"campaign_id": campaign_id, "status": campaign["status"], "total": total,
            "pending": counts.get("pending", 0), "sending": counts.get("sending", 0),
            "sent": counts.get("sent", 0), "failed": counts.get("failed", 0)}

@app.post("/api/campaigns/{campaign_id}/process")
async def process_campaign(campaign_id: int, payload: CampaignProcessRequest):
    limit = min(payload.limit, MAX_SEND_PER_REQUEST, 20)
    with db() as con:
        campaign = con.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if not campaign:
            raise HTTPException(404, "Campaign not found.")
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=STALE_SENDING_MINUTES)).isoformat()
        # A batch that died mid-send is marked failed, never re-sent automatically (avoids duplicate emails).
        con.execute("""UPDATE campaign_recipients SET status='failed', updated_at=?,
                       error='Interrupted while sending; not retried automatically to avoid a duplicate email.'
                       WHERE campaign_id=? AND status='sending' AND updated_at<?""", (now(), campaign_id, cutoff))
        rows = con.execute("""SELECT cr.id AS queue_id, c.* FROM campaign_recipients cr
                              JOIN contacts c ON c.id=cr.contact_id
                              WHERE cr.campaign_id=? AND cr.status='pending' AND c.consent=1
                              ORDER BY cr.id LIMIT ?""", (campaign_id, limit)).fetchall()
        selected = []
        for row in rows:
            # Atomic claim: only the request that flips pending -> sending may send this recipient.
            claimed = con.execute("UPDATE campaign_recipients SET status='sending', error='', updated_at=? WHERE id=? AND status='pending'",
                                  (now(), row["queue_id"]))
            if claimed.rowcount == 1:
                selected.append(dict(row))
        if not selected:
            counts = {r["status"]: r["n"] for r in con.execute(
                "SELECT status, COUNT(*) n FROM campaign_recipients WHERE campaign_id=? GROUP BY status", (campaign_id,)).fetchall()}
            if not counts.get("sending", 0):
                con.execute("UPDATE campaigns SET status='completed', updated_at=? WHERE id=?", (now(), campaign_id))
            return {"campaign_id": campaign_id, "processed": 0, "sent": counts.get("sent", 0), "failed": counts.get("failed", 0), "pending": counts.get("pending", 0), "done": True, "errors": []}
        con.execute("UPDATE campaigns SET status='sending', updated_at=? WHERE id=?", (now(), campaign_id))
        templates = dict(campaign)
    try:
        campaign_attachments = json.loads(templates.get("attachments_json") or "[]")
    except ValueError:
        campaign_attachments = []
    sent = failed = 0
    batch_errors = []
    for contact in selected:
        subject = personalize_template(templates["subject_template"], contact)
        body = personalize_template(templates["body_template"], contact)
        try:
            message_id = await send_via_brevo(contact["email"], contact["name"], subject, body, campaign_attachments)
            with db() as con:
                con.execute("UPDATE campaign_recipients SET status='sent', provider_message_id=?, error='', updated_at=? WHERE id=?",
                            (message_id, now(), contact["queue_id"]))
            sent += 1
        except Exception as exc:
            error = str(exc)[:1000]
            with db() as con:
                con.execute("UPDATE campaign_recipients SET status='failed', error=?, updated_at=? WHERE id=?",
                            (error, now(), contact["queue_id"]))
            failed += 1
            if len(batch_errors) < 5:
                batch_errors.append({"email": contact["email"], "error": error})
    with db() as con:
        counts = {r["status"]: r["n"] for r in con.execute(
            "SELECT status, COUNT(*) n FROM campaign_recipients WHERE campaign_id=? GROUP BY status", (campaign_id,)).fetchall()}
        pending = counts.get("pending", 0)
        sending = counts.get("sending", 0)
        status = "completed" if pending == 0 and sending == 0 else "queued"
        con.execute("UPDATE campaigns SET status=?, updated_at=? WHERE id=?", (status, now(), campaign_id))
    return {"campaign_id": campaign_id, "processed": len(selected), "sent_this_batch": sent,
            "failed_this_batch": failed, "sent": counts.get("sent", 0), "failed": counts.get("failed", 0),
            "pending": pending, "done": pending == 0 and sending == 0, "errors": batch_errors}

@app.get("/api/campaigns/{campaign_id}/failures")
def campaign_failures(campaign_id: int):
    with db() as con:
        rows = con.execute("""SELECT c.email, cr.error FROM campaign_recipients cr
                              JOIN contacts c ON c.id=cr.contact_id
                              WHERE cr.campaign_id=? AND cr.status='failed' ORDER BY cr.id LIMIT 200""", (campaign_id,)).fetchall()
    return [dict(r) for r in rows]

@app.post("/api/send-approved")
async def send_approved(payload: SendRequest):
    if len(payload.draft_ids) > MAX_SEND_PER_REQUEST:
        raise HTTPException(400, f"Send at most {MAX_SEND_PER_REQUEST} emails per request. Adjust this only within provider limits.")
    results = []
    for did in payload.draft_ids:
        with db() as con:
            row = con.execute("""SELECT d.*, c.email, c.name, c.consent
                                 FROM drafts d JOIN contacts c ON c.id=d.contact_id
                                 WHERE d.id=?""", (did,)).fetchone()
            if not row:
                results.append({"id": did, "status": "failed", "email": "", "error": "Draft not found"})
                continue
            draft = dict(row)
            if draft["status"] != "approved":
                results.append({"id": did, "status": "skipped", "email": draft["email"], "error": "Draft is not approved"})
                continue
            if not draft["consent"]:
                results.append({"id": did, "status": "skipped", "email": draft["email"], "error": "Recipient consent not recorded"})
                continue
            con.execute("UPDATE drafts SET status='sending', error='', updated_at=? WHERE id=?", (now(), did))
        try:
            message_id = await send_via_brevo(draft["email"], draft["name"], draft["subject"], draft["body"])
            with db() as con:
                con.execute("UPDATE drafts SET status='sent', provider_message_id=?, error='', updated_at=? WHERE id=?",
                            (message_id, now(), did))
            results.append({"id": did, "status": "sent", "email": draft["email"], "provider_message_id": message_id})
        except HTTPException:
            with db() as con:
                con.execute("UPDATE drafts SET status='failed', error='Email provider is not configured', updated_at=? WHERE id=?", (now(), did))
            raise
        except Exception as exc:
            error = str(exc)[:1000]
            with db() as con:
                con.execute("UPDATE drafts SET status='failed', error=?, updated_at=? WHERE id=?", (error, now(), did))
            results.append({"id": did, "status": "failed", "email": draft["email"], "error": error})
    return {"results": results}

@app.get("/api/stats")
def stats():
    with db() as con:
        contacts = con.execute("SELECT COUNT(*) n FROM contacts").fetchone()["n"]
        consented = con.execute("SELECT COUNT(*) n FROM contacts WHERE consent=1").fetchone()["n"]
        counts = con.execute("SELECT status, COUNT(*) n FROM drafts GROUP BY status").fetchall()
    return {"contacts": contacts, "consented_contacts": consented, "drafts": {r["status"]: r["n"] for r in counts}}
