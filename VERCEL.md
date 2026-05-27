# Deploy OptiPDP on Vercel (one-time / demo)

> **Note:** Mode 1 analysis takes 2–4 minutes. Vercel serverless max is **60s** (Pro) or **10s** (Hobby). Long audits may timeout — use **Render** for production. This setup is fine for login, History, and quick API tests.

## 1. Push code (already on GitHub)

Repo: https://github.com/PrinceKeshri966/PDP-DEV

## 2. Import on Vercel

1. Go to [vercel.com](https://vercel.com) → **Add New** → **Project**
2. Import **PrinceKeshri966/PDP-DEV**
3. **Critical settings** (override defaults):
   - Framework Preset: **Other**
   - Root Directory: **.** (leave empty — repo root)
   - Build Command: **leave empty** (uses `vercel.json`)
   - Output Directory: **public** (or leave empty if `vercel.json` sets it)
   - Turn **OFF** any dashboard overrides that conflict with `vercel.json`
4. Click **Deploy**

## 3. Environment variables

In Vercel → Project → **Settings** → **Environment Variables**, add:

| Variable | Example |
|----------|---------|
| `DATABASE_URL` | `postgresql+asyncpg://user:pass@ep-....neon.tech/neondb?ssl=require` |
| `ANTHROPIC_API_KEY` | your key |
| `SECRET_KEY` | long random string |
| `APP_ENV` | `production` |
| `DEBUG` | `false` |
| `DEV_AUTH_BYPASS` | `false` |
| `SKIP_PLAYWRIGHT` | `true` |
| `GOOGLE_CLIENT_ID` | from Google Cloud |
| `GOOGLE_CLIENT_SECRET` | from Google Cloud |
| `APP_BASE_URL` | `https://YOUR-PROJECT.vercel.app` |
| `GOOGLE_REDIRECT_URI` | `https://YOUR-PROJECT.vercel.app/api/v1/auth/google/callback` |
| `ALLOWED_ORIGINS` | `https://YOUR-PROJECT.vercel.app` |
| `JINA_API_KEY` | optional, for scraping |

After first deploy, replace `YOUR-PROJECT` with your real Vercel URL and redeploy.

## 4. Google OAuth

Google Cloud Console → OAuth client:

- **Authorized JavaScript origins:** `https://YOUR-PROJECT.vercel.app`
- **Authorized redirect URIs:** `https://YOUR-PROJECT.vercel.app/api/v1/auth/google/callback`

## 5. Deploy

Click **Deploy**. Or locally:

```bash
npm i -g vercel
vercel login
vercel --prod
```

## 6. Verify

- `https://YOUR-PROJECT.vercel.app` → OptiPDP UI
- `https://YOUR-PROJECT.vercel.app/health` → `{"status":"ok"}`
- Sign in with Google → run a short test

## Limits on Vercel

| Feature | Vercel | Render |
|---------|--------|--------|
| Full Mode 1 (10 agents) | May timeout | Works |
| SSE streaming | Limited | Works |
| Playwright scrape | No | Optional |

For production, keep **Render** as primary; use Vercel for demos if needed.
