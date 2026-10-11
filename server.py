"""
Web UI + JSON API for the get2short direct-link generator.

Railway / Docker:
    PORT is injected by the platform. Listens on 0.0.0.0.
    Set env vars (see README_RAILWAY.md):
      G2S_PROXY, G2S_SOLVER_URL, G2S_SOLVER_API_KEY, G2S_HEADLESS, ...

Local:
    python server.py            -> http://127.0.0.1:8765
    GET  /api/resolve?url=...   -> {"url": "<destination>", ...}
"""
import asyncio
import json
import os
import sys
import time
import uuid
import webbrowser

from aiohttp import web

from g2s.engine import Bypasser, BypassError, Config

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
CFG_PATH = os.environ.get("G2S_CONFIG", os.path.join(HERE, "config.json"))
CACHE_PATH = os.path.join(HERE, "results.json")


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    v = os.environ.get(name)
    if v is None:
        return default
    try:
        return float(v)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    if v is None:
        return default
    try:
        return int(v)
    except ValueError:
        return default


with open(CFG_PATH, encoding="utf-8") as f:
    RAW = json.load(f)

cfg = Config.load(CFG_PATH)

# --- Railway / env overrides (highest priority) ---
if os.environ.get("G2S_SOLVER_URL"):
    cfg.solver_url = os.environ["G2S_SOLVER_URL"].rstrip("/")
if os.environ.get("G2S_SOLVER_API_KEY"):
    cfg.solver_api_key = os.environ["G2S_SOLVER_API_KEY"]
if os.environ.get("G2S_PROXY"):
    cfg.proxy = os.environ["G2S_PROXY"].strip() or None
if "G2S_HEADLESS" in os.environ:
    cfg.headless = _env_bool("G2S_HEADLESS", True)
if "G2S_SHOW_BROWSER" in os.environ:
    cfg.show_browser = _env_bool("G2S_SHOW_BROWSER", False)
if "G2S_BROWSER_CHANNEL" in os.environ:
    ch = os.environ["G2S_BROWSER_CHANNEL"].strip()
    cfg.browser_channel = ch or None
if os.environ.get("G2S_PROFILE_DIR"):
    cfg.profile_dir = os.environ["G2S_PROFILE_DIR"]
if os.environ.get("G2S_DEBUG_DIR"):
    cfg.debug_dir = os.environ["G2S_DEBUG_DIR"]
if "G2S_DEBUG" in os.environ:
    cfg.debug = _env_bool("G2S_DEBUG", False)
if "G2S_ACCEL_FACTOR" in os.environ:
    cfg.accel_factor = _env_float("G2S_ACCEL_FACTOR", 0.1)
if "G2S_TIMEOUT" in os.environ:
    cfg.timeout = _env_int("G2S_TIMEOUT", 300)

# CLI flags still work locally
if "--show" in sys.argv:
    cfg.show_browser = True
    cfg.headless = False
if "--debug" in sys.argv:
    cfg.debug = True

# Host / port: Railway sets PORT; always bind 0.0.0.0 in containers
HOST = os.environ.get("HOST") or os.environ.get("G2S_HOST") or RAW.get("host", "0.0.0.0")
if os.environ.get("PORT"):
    PORT = int(os.environ["PORT"])
else:
    PORT = int(RAW.get("port", 8765))
CACHE_HOURS = float(os.environ.get("G2S_CACHE_HOURS", RAW.get("cache_hours", 24)))
NO_OPEN = (
    _env_bool("G2S_NO_OPEN", False)
    or ("--no-open" in sys.argv)
    or bool(os.environ.get("RAILWAY_ENVIRONMENT"))
    or (os.name != "nt" and not cfg.show_browser)
)

bypasser = Bypasser(cfg)
jobs: dict[str, dict] = {}


def load_cache():
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


cache = load_cache()


def save_cache():
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=1)
    except Exception as e:
        print("cache save failed:", e, flush=True)


def norm(url: str) -> str:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url.rstrip("/")


async def run_job(job):
    def log(msg):
        line = f"[{time.time() - job['t0']:5.1f}s] {msg}"
        job["log"].append(line)
        print(line, flush=True)
        job["event"].set()

    key = norm(job["source"])
    hit = cache.get(key)
    if hit and not job["force"] and time.time() - hit["at"] < CACHE_HOURS * 3600:
        log("Served from cache (use 'Force' to resolve again)")
        job["result"] = {**hit, "cached": True}
    else:
        job["status"] = "running"
        job["event"].set()
        try:
            res = await bypasser.resolve(key, log=log)
            res["at"] = time.time()
            cache[key] = res
            save_cache()
            job["result"] = {**res, "cached": False}
        except BypassError as e:
            job["error"] = str(e)
            log(f"✖ {e}")
        except Exception as e:  # unexpected
            job["error"] = f"{type(e).__name__}: {e}"
            log(f"✖ {job['error']}")
    job["status"] = "done"
    job["event"].set()


def new_job(url, force=False):
    jid = uuid.uuid4().hex[:10]
    job = {"id": jid, "source": url, "force": force, "status": "queued", "log": [],
           "result": None, "error": None, "t0": time.time(), "event": asyncio.Event()}
    jobs[jid] = job
    job["task"] = asyncio.ensure_future(run_job(job))
    return job


def public(job):
    return {k: job[k] for k in ("id", "source", "status", "result", "error", "log")}


# ------------------------------------------------------------------- routes
async def index(_):
    with open(os.path.join(HERE, "web", "index.html"), encoding="utf-8") as f:
        return web.Response(text=f.read(), content_type="text/html")


async def create_job(req):
    data = await req.json()
    urls = [u.strip() for u in str(data.get("url", "")).splitlines() if u.strip()]
    if not urls:
        return web.json_response({"error": "no url"}, status=400)
    return web.json_response({"jobs": [new_job(u, bool(data.get("force")))["id"] for u in urls]})


async def job_events(req):
    job = jobs.get(req.match_info["jid"])
    if not job:
        raise web.HTTPNotFound()
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream",
                                       "Cache-Control": "no-cache"})
    await resp.prepare(req)
    sent = 0
    try:
        while True:
            job["event"].clear()
            while sent < len(job["log"]):
                await resp.write(f"data: {json.dumps({'log': job['log'][sent]})}\n\n".encode())
                sent += 1
            if job["status"] == "done":
                await resp.write(f"data: {json.dumps({'done': public(job)})}\n\n".encode())
                break
            try:
                await asyncio.wait_for(job["event"].wait(), 15)
            except asyncio.TimeoutError:
                await resp.write(b": ping\n\n")
    except (ConnectionResetError, asyncio.CancelledError, aiohttp.ClientConnectionError):
        pass
    return resp


async def resolve_api(req):
    url = req.query.get("url")
    if not url:
        return web.json_response({"error": "pass ?url="}, status=400)
    job = new_job(url, req.query.get("force") in ("1", "true"))
    await job["task"]
    if job["error"]:
        return web.json_response({"error": job["error"], "log": job["log"]}, status=502)
    return web.json_response(job["result"])


async def history(_):
    items = sorted(({"source": k, **v} for k, v in cache.items()), key=lambda x: -x["at"])
    return web.json_response(items[:50])


async def clear_history(_):
    cache.clear()
    save_cache()
    return web.json_response({"ok": True})


async def health(_):
    try:
        h = await bypasser.solver.health()
        return web.json_response({"solver": "ok", "detail": h, "solver_url": cfg.solver_url,
                                  "status": "ok", "proxy_set": bool(cfg.proxy)})
    except Exception as e:
        return web.json_response({"solver": "down", "detail": str(e), "solver_url": cfg.solver_url,
                                  "status": "degraded", "proxy_set": bool(cfg.proxy)})


def mask_proxy(p):
    import re as _re
    return _re.sub(r"(://[^:/@]+:)[^@]+@", r"\1•••@", p) if p else None


def save_raw():
    try:
        RAW["proxy"] = cfg.proxy
        with open(CFG_PATH, "w", encoding="utf-8") as f:
            json.dump(RAW, f, indent=2)
    except Exception as e:
        print("could not save config:", e, flush=True)


async def get_settings(_):
    return web.json_response({"proxy": mask_proxy(cfg.proxy), "has_proxy": bool(cfg.proxy),
                              "solver_url": cfg.solver_url, "accel_factor": cfg.accel_factor,
                              "headless": cfg.headless})


async def post_settings(req):
    data = await req.json()
    if "proxy" in data:
        if bypasser.busy:
            return web.json_response({"error": "a job is running — try again when it finishes"}, status=409)
        proxy = (data.get("proxy") or "").strip() or None
        await bypasser.set_proxy(proxy)
        save_raw()
    return await get_settings(req)


async def proxy_test(_):
    import aiohttp
    import base64
    from g2s.engine import parse_proxy
    px = cfg.proxy
    try:
        kw = {}
        if px:
            d = parse_proxy(px)
            kw["proxy"] = d["server"]
            if d.get("username"):
                token = base64.b64encode(f"{d['username']}:{d.get('password', '')}".encode()).decode()
                kw["proxy_headers"] = {"Proxy-Authorization": f"Basic {token}"}
        async with aiohttp.ClientSession() as s:
            async with s.get("https://ipinfo.io/json", timeout=aiohttp.ClientTimeout(total=20), **kw) as r:
                info = await r.json(content_type=None)
        dc = any(w in (info.get("org") or "").lower() for w in
                 ("google", "amazon", "microsoft", "digitalocean", "ovh", "hetzner", "oracle",
                  "linode", "railway", "cloudflare", "akamai"))
        return web.json_response({"ok": True, "via_proxy": bool(px), "ip": info.get("ip"),
                                  "city": info.get("city"), "region": info.get("region"),
                                  "country": info.get("country"), "org": info.get("org"),
                                  "datacenter": dc})
    except Exception as e:
        return web.json_response({"ok": False, "error": f"{type(e).__name__}: {e}"})


async def screenshot(_):
    p = bypasser.current_page
    if p is None:
        return web.Response(status=204)
    try:
        png = await p.screenshot(type="jpeg", quality=55, timeout=5000)
        return web.Response(body=png, content_type="image/jpeg", headers={"Cache-Control": "no-store"})
    except Exception:
        return web.Response(status=204)


async def on_startup(app):
    bypasser._ensure_display()
    print(f"Starting browser (headless={cfg.headless}, channel={cfg.browser_channel!r})…", flush=True)
    if cfg.proxy:
        print(f"Proxy configured: {mask_proxy(cfg.proxy)}", flush=True)
    else:
        print("Proxy: none configured at startup (can be set via G2S_PROXY or Web UI).", flush=True)
    await bypasser.start()
    url = f"http://{HOST}:{PORT}/"
    print(f"\n  Direct-link generator ready → {url}\n", flush=True)
    if not NO_OPEN:
        try:
            webbrowser.open(url)
        except Exception:
            pass


async def on_cleanup(app):
    await bypasser.close()


app = web.Application()
app.router.add_get("/", index)
app.router.add_post("/api/jobs", create_job)
app.router.add_get("/api/jobs/{jid}/events", job_events)
app.router.add_get("/api/resolve", resolve_api)
app.router.add_get("/api/history", history)
app.router.add_delete("/api/history", clear_history)
app.router.add_get("/api/health", health)
app.router.add_get("/api/settings", get_settings)
app.router.add_post("/api/settings", post_settings)
app.router.add_get("/api/proxy-test", proxy_test)
app.router.add_get("/api/screenshot", screenshot)
app.on_startup.append(on_startup)
app.on_cleanup.append(on_cleanup)

if __name__ == "__main__":
    print(f"Listening on {HOST}:{PORT}", flush=True)
    web.run_app(app, host=HOST, port=PORT, print=None)
