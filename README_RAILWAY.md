# G2S → Railway Deploy Guide

Yeh project **Railway.com** pe deploy ke liye ready hai.

> ⚠️ **Zaroori baat:** get2short datacenter IPs (Railway, AWS, etc.) ko block karta hai (`ip_risk_blocked`).  
> Isliye **residential / home proxy** lagana mandatory hai, warna links generate nahi honge.

---

## 1. Railway pe deploy (recommended – Docker)

### Step A – Repo banao
1. Is folder ko GitHub pe push karo (ya Railway pe directly upload).
2. [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub** (ya "Empty Project" + this folder).

### Step B – Variables set karo (Settings → Variables)

| Variable | Example / Value | Required? |
|----------|-----------------|-----------|
| `G2S_PROXY` | `http://user:pass@host:port` | **Haan** (residential) |
| `G2S_SOLVER_URL` | `http://hayabusa.proxy.rlwy.net:46794` | Haan (apna solver) |
| `G2S_SOLVER_API_KEY` | (agar solver pe key lagi ho) | Optional |
| `G2S_HEADLESS` | `0` | Recommended (uses built-in Xvfb virtual display) |
| `G2S_BROWSER_CHANNEL` | (khali chhodo) | Recommended |
| `G2S_NO_OPEN` | `1` | Auto on Railway |
| `PORT` | Railway khud set karta hai | Auto |

**Proxy sources (examples):**
- Apna home PC se Pinggy tunnel (`tools/home_proxy.py` se)
- Paid residential proxy (Bright Data, IPRoyal, PacketStream, etc.)

### Step C – Deploy
Railway `Dockerfile` detect karega → build → deploy.  
Healthcheck: `GET /api/health`

Deploy hone ke baad public URL milegi, jaise:
`https://g2s-xxxx.up.railway.app`

---

## 2. Local Docker test (optional)

```bash
docker build -t g2s .
docker run --rm -p 8080:8080 \
  -e G2S_PROXY="http://user:pass@host:port" \
  -e G2S_SOLVER_URL="http://your-solver:port" \
  g2s
```

Browser: http://localhost:8080

---

## 3. API usage (deploy ke baad)

```bash
# Single resolve
curl "https://YOUR-RAILWAY-URL/api/resolve?url=https://get2short.com/4mEa"

# Health
curl "https://YOUR-RAILWAY-URL/api/health"

# Proxy test (IP check)
curl "https://YOUR-RAILWAY-URL/api/proxy-test"
```

UI bhi same URL pe open hogi.

---

## 4. Important notes

1. **Proxy must be residential** – `api/proxy-test` response mein `"datacenter": false` hona chahiye.
2. Headless mode Cloudflare ko kabhi-kabhi fail karta hai. Agar fail ho to:
   - Better residential proxy try karo
   - Ya `G2S_HEADLESS=0` (lekin Railway pe Xvfb chahiye – Dockerfile me abhi nahi hai)
3. Solver alag service pe hona chahiye (jaise pehle se Railway pe `hayabusa` wala).
4. Free Pinggy tunnels 60 min baad change ho jate hain – naya URL `G2S_PROXY` me update karna padega.

---

## File structure (deploy ke liye zaroori)

```
├── Dockerfile          ← Railway isse build karega
├── railway.toml
├── server.py           ← PORT + env aware
├── config.json
├── requirements.txt
├── g2s/                ← engine, scripts, solver
├── web/index.html
└── README_RAILWAY.md
```

`run.bat`, `setup.bat`, `tests/`, `tools/` optional hain – Railway pe zarurat nahi.
