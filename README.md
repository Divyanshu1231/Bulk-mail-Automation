# AI Email Automation — Full Project

React + Vite frontend, FastAPI backend, SQLite database, Gemini reusable-template generation, and Brevo batch sending.

## Project layout

- `frontend/` — React/Vite app for CSV import, contact authorization, one reusable email template, and batch campaigns.
- `backend/` — FastAPI API and SQLite storage.
- `backend/.env.example` — environment variable template. Never commit real API keys.

## Local development (Windows)

### 1. Start backend

```powershell
cd backend
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `backend/.env` with real Gemini and Brevo keys and a verified sender email. Then run:

```powershell
uvicorn app:app --reload --host 127.0.0.1 --port 8000
```

Health check: http://127.0.0.1:8000/api/health

### 2. Start frontend (new terminal)

```powershell
cd frontend
npm install
Copy-Item .env.example .env
npm run dev
```

Open the URL Vite prints (normally http://127.0.0.1:5173).

## Deploy everything on Render (frontend + backend)

Dono services Render par chalti hain. Netlify ki zaroorat nahi.

### Option A: Blueprint (recommended)

1. Code GitHub par push karo (repo root me `render.yaml` hona chahiye).
2. Render Dashboard → **New → Blueprint** → repo select karo → **Apply**.
3. Secrets daalo: `GEMINI_API_KEY`, `BREVO_API_KEY`, `BREVO_SENDER_EMAIL`.
4. Do services banengi:
   - `bulk-mail-automation` (backend, Python Web Service)
   - `bulk-mail-frontend` (React Static Site)

### Option B: Manual

**Backend (New → Web Service)**
- Root Directory: `backend`
- Build: `pip install -r requirements.txt`
- Start: `uvicorn app:app --host 0.0.0.0 --port $PORT`
- Health Check Path: `/api/health`
- Env: `backend/.env.example` se copy karo + `PYTHON_VERSION=3.12.8`

**Frontend (New → Static Site)**
- Root Directory: `frontend`
- Build: `npm install && npm run build`
- Publish Directory: `dist`
- Env: `VITE_API_BASE_URL=https://<backend-service>.onrender.com` (sirf origin, `/api` nahi)
- Redirects/Rewrites: `/*` → `/index.html` (Rewrite)

### Important

- Service ke naam alag rakhe to `render.yaml` me `VITE_API_BASE_URL`, `FRONTEND_ORIGIN` aur `CORS_ORIGINS` ko asli URLs se update karo.
- `CORS_ORIGINS` me frontend ka exact URL ho, trailing `/` ke bina. Change ke baad backend redeploy karo.
- `VITE_API_BASE_URL` change karne par frontend ko dobara build/deploy karo.
- Free backend idle hone par sleep karta hai; pehli request 30-60s le sakti hai (frontend auto-retry karta hai).
- Free plan par SQLite data restart par reset hota hai. Persist karna ho to paid plan + disk lo (`render.yaml` me comment diya hai).
- Health check: `https://<backend>.onrender.com/api/health`

## Bulk sending notes

- Gemini generates one reusable template; placeholders `{{name}}`, `{{company}}`, and `{{role}}` are personalized per contact.
- Campaign processing uses batches; the provider's plan quota still applies. Batch sending does not bypass Brevo limits.
- Only mark consent when valid permission exists. Test with a few authorized contacts first.
- “Accepted by Brevo” is not the same as inbox delivery.
