"""
TheDrunkBots G2S — Password-Protected Web Admin Panel, Cloud API & Telegram Bot Runner.

Features:
  - Unified MongoDB Atlas + config.json real-time sync
  - Telegram Bot runner with live progress bar, Force-Subscribe, and in-bot Admin Panel
  - Password-protected Web Admin Panel (live preview disabled; full CRUD on Tunnel/Proxy,
    Cloud Cache, Force-Sub Channels, Error Logs, Bot Users, Broadcast, and Credentials)
  - Automatic Owner notification on Telegram when tunnel expires or a link errors
"""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import uuid
import webbrowser

import aiohttp
from aiohttp import web

from g2s.bot import TelegramBot
from g2s.db import Database, norm_url
from g2s.engine import Bypasser, BypassError, Config, parse_proxy

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
CFG_PATH = os.environ.get("G2S_CONFIG", os.path.join(HERE, "config.json"))
CACHE_PATH = os.path.join(HERE, "results.json")

db = Database(CFG_PATH, CACHE_PATH)
cfg = Config.load(CFG_PATH)


def _apply_db_settings_to_cfg():
    s = db.settings
    cfg.solver_url = (s.get("solver_url") or "https://cf.vltcx.eu.cc").rstrip("/")
    cfg.solver_api_key = s.get("solver_api_key")
    cfg.proxy = None if s.get("proxy_expired") else s.get("proxy")
    cfg.accel_factor = float(s.get("accel_factor", 0.1))
    cfg.timeout = int(s.get("timeout", 300))
    cfg.stuck_seconds = int(s.get("stuck_seconds", 100))
    if s.get("no_accel_hosts"):
        cfg.no_accel_hosts = list(s["no_accel_hosts"])


_apply_db_settings_to_cfg()

if "--show" in sys.argv:
    cfg.show_browser = True
    cfg.headless = False
if "--debug" in sys.argv:
    cfg.debug = True

HOST = os.environ.get("HOST") or os.environ.get("G2S_HOST") or "0.0.0.0"
PORT = int(os.environ.get("PORT") or db.settings.get("port") or 8080)
NO_OPEN = (
    os.environ.get("G2S_NO_OPEN", "").strip().lower() in ("1", "true", "yes")
    or ("--no-open" in sys.argv)
    or bool(os.environ.get("RAILWAY_ENVIRONMENT"))
    or (os.name != "nt" and not cfg.show_browser)
)

bypasser = Bypasser(cfg)
jobs: dict[str, dict] = {}
SESSION_SECRET = hashlib.sha256(f"g2s-secret-{os.getpid()}-{CFG_PATH}".encode()).hexdigest()


def make_auth_token(password: str) -> str:
    return hmac.new(SESSION_SECRET.encode(), (password or "").encode(), hashlib.sha256).hexdigest()


def is_req_authenticated(req: web.Request) -> bool:
    expected = make_auth_token(str(db.settings.get("admin_password") or "admin123"))
    auth_hdr = (req.headers.get("Authorization") or "").strip()
    if auth_hdr.startswith("Bearer "):
        tok = auth_hdr[7:].strip()
        if hmac.compare_digest(tok, expected):
            return True
    cookie_tok = (req.cookies.get("g2s_admin_token") or "").strip()
    if cookie_tok and hmac.compare_digest(cookie_tok, expected):
        return True
    key_q = (req.query.get("key") or req.query.get("password") or "").strip()
    if key_q and key_q == str(db.settings.get("admin_password") or "admin123"):
        return True
    return False


async def test_proxy_status(px_override: str | None = "__current__") -> dict:
    px = (db.settings.get("proxy") or cfg.proxy) if px_override == "__current__" else px_override
    try:
        kw = {}
        if px:
            d = parse_proxy(px)
            kw["proxy"] = d["server"]
            if d.get("username"):
                raw_auth = f"{d['username']}:{d.get('password', '')}".encode("utf-8")
                kw["proxy_headers"] = {
                    "Proxy-Authorization": "Basic " + base64.b64encode(raw_auth).decode("ascii")
                }
        async with aiohttp.ClientSession() as s:
            async with s.get("https://ipinfo.io/json", timeout=aiohttp.ClientTimeout(total=18), **kw) as r:
                info = await r.json(content_type=None)
        dc = any(
            w in (info.get("org") or "").lower()
            for w in (
                "google", "amazon", "microsoft", "digitalocean", "ovh", "hetzner",
                "oracle", "linode", "railway", "cloudflare", "akamai", "choopa", "vultr",
            )
        )
        if px and db.settings.get("proxy_expired"):
            bot._alerted_expired_proxy = None
            await db.update_settings({"proxy_expired": False, "proxy_expired_notified_for": ""}, notify=False)
        return {
            "ok": True,
            "via_proxy": bool(px),
            "ip": info.get("ip"),
            "city": info.get("city"),
            "region": info.get("region"),
            "country": info.get("country"),
            "org": info.get("org"),
            "datacenter": dc,
        }
    except Exception as e:
        return {"ok": False, "via_proxy": bool(px), "error": f"{type(e).__name__}: {e}"}


async def set_system_proxy(new_proxy: str | None):
    clean = (new_proxy or "").strip() or None
    bot._alerted_expired_proxy = None
    await db.update_settings(
        {"proxy": clean, "proxy_expired": False, "proxy_expired_notified_for": ""},
        notify=False,
    )
    cfg.proxy = clean
    await bypasser.set_proxy(clean)


_inflight: dict[str, dict] = {}


async def resolve_link_core(
    url: str,
    force: bool = False,
    origin: str = "web",
    user_id: int | None = None,
    username: str | None = None,
    extra_log_cb=None,
) -> dict:
    key = norm_url(url)
    hit = await db.get_cache(key, force=force)
    if hit:
        if extra_log_cb:
            extra_log_cb("Served from cloud cache")
        return hit

    # Deduplicate simultaneous requests for the exact same URL
    if not force and key in _inflight:
        existing = _inflight[key]
        if extra_log_cb:
            for prev_msg in existing["raw_logs"]:
                try:
                    extra_log_cb(prev_msg)
                except Exception:
                    pass
            existing["cbs"].append(extra_log_cb)
        return await asyncio.shield(existing["fut"])

    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    cbs = [extra_log_cb] if extra_log_cb else []
    raw_logs: list[str] = []
    if not force:
        _inflight[key] = {"fut": fut, "cbs": cbs, "raw_logs": raw_logs}

    log_lines: list[str] = []
    t0 = time.time()

    def log_fn(msg: str):
        raw_logs.append(msg)
        line = f"[{time.time() - t0:5.1f}s] {msg}"
        log_lines.append(line)
        print(line, flush=True)
        for cb in list(cbs):
            if cb:
                try:
                    cb(msg)
                except Exception:
                    pass

    try:
        res = await bypasser.resolve(key, log=log_fn)
        res["at"] = time.time()
        await db.set_cache(key, res)
        await db.increment_stat("total_bypasses", 1)
        out = {**res, "cached": False}
        if not fut.done():
            fut.set_result(out)
        return out
    except Exception as e:
        err_str = str(e) if isinstance(e, BypassError) else f"{type(e).__name__}: {e}"
        log_fn(f"✖ {err_str}")
        entry = await db.add_error_log(
            source=key,
            error=err_str,
            log_lines=log_lines,
            origin=origin,
            user_id=user_id,
            username=username,
        )
        asyncio.create_task(bot.notify_error_log(entry))
        if not fut.done():
            fut.set_exception(e)
        raise
    finally:
        if _inflight.get(key, {}).get("fut") is fut:
            _inflight.pop(key, None)


bot = TelegramBot(
    db=db,
    resolve_fn=resolve_link_core,
    proxy_test_fn=test_proxy_status,
    set_proxy_fn=set_system_proxy,
)


async def _on_proxy_expired(expired_px: str | None, err_msg: str):
    await bot.notify_tunnel_expired(expired_px, err_msg)


bypasser.on_proxy_expired = _on_proxy_expired


async def _on_settings_changed(changed_keys: set, new_settings: dict):
    _apply_db_settings_to_cfg()
    if "proxy" in changed_keys and cfg.proxy != bypasser.cfg.proxy and not bypasser.busy:
        await bypasser.set_proxy(cfg.proxy)
    if "telegram_bot_token" in changed_keys:
        await bot.reload_if_needed()


db.on_settings_change(_on_settings_changed)


# ------------------------------------------------------------------ Job runner
async def run_job(job: dict):
    def on_log(msg: str):
        line = f"[{time.time() - job['t0']:5.1f}s] {msg}"
        job["log"].append(line)
        job["event"].set()

    job["status"] = "running"
    job["event"].set()
    try:
        res = await resolve_link_core(
            job["source"],
            force=job["force"],
            origin="admin-web",
            extra_log_cb=on_log,
        )
        job["result"] = res
    except BypassError as e:
        job["error"] = str(e)
    except Exception as e:
        job["error"] = f"{type(e).__name__}: {e}"
    job["status"] = "done"
    job["event"].set()


def new_job(url: str, force: bool = False) -> dict:
    jid = uuid.uuid4().hex[:10]
    job = {
        "id": jid,
        "source": norm_url(url),
        "force": force,
        "status": "queued",
        "log": [],
        "result": None,
        "error": None,
        "t0": time.time(),
        "event": asyncio.Event(),
    }
    jobs[jid] = job
    job["task"] = asyncio.ensure_future(run_job(job))
    return job


def public_job(job: dict) -> dict:
    return {k: job[k] for k in ("id", "source", "status", "result", "error", "log")}


def mask_proxy(p: str | None) -> str | None:
    import re as _re
    return _re.sub(r"(://[^:/@]+:)[^@]+@", r"\1•••@", p) if p else None


# ------------------------------------------------------------------ Middleware
@web.middleware
async def auth_middleware(req: web.Request, handler):
    path = req.path
    if path in ("/", "/api/health", "/api/auth/login", "/api/auth/logout", "/api/auth/me"):
        return await handler(req)
    if path.startswith("/api/"):
        if not is_req_authenticated(req):
            return web.json_response({"error": "Unauthorized — admin password required"}, status=401)
    return await handler(req)


# ---------------------------------------------------------------------- Routes
async def index(_: web.Request):
    with open(os.path.join(HERE, "web", "index.html"), encoding="utf-8") as f:
        return web.Response(text=f.read(), content_type="text/html")


async def auth_login(req: web.Request):
    data = await req.json()
    pw = str(data.get("password") or "").strip()
    expected_pw = str(db.settings.get("admin_password") or "admin123")
    if pw != expected_pw:
        return web.json_response({"ok": False, "error": "Invalid admin password"}, status=401)
    tok = make_auth_token(expected_pw)
    resp = web.json_response({
        "ok": True,
        "token": tok,
        "brand": db.settings.get("brand_name", "TheDrunkBots"),
    })
    resp.set_cookie("g2s_admin_token", tok, max_age=86400 * 30, httponly=True, samesite="Lax")
    return resp


async def auth_logout(_: web.Request):
    resp = web.json_response({"ok": True})
    resp.del_cookie("g2s_admin_token")
    return resp


async def auth_me(req: web.Request):
    return web.json_response({
        "authenticated": is_req_authenticated(req),
        "brand": db.settings.get("brand_name", "TheDrunkBots"),
    })


async def health(_: web.Request):
    try:
        h = await bypasser.solver.health()
        solver_st = "ok"
    except Exception as e:
        h = str(e)
        solver_st = "down"
    return web.json_response({
        "status": "ok" if solver_st == "ok" else "degraded",
        "solver": solver_st,
        "detail": h,
        "solver_url": cfg.solver_url,
        "proxy_set": bool(cfg.proxy),
        "proxy_expired": bool(db.settings.get("proxy_expired")),
        "mongo_connected": db.connected,
        "bot_running": bot.running,
        "bot_username": (bot.bot_info or {}).get("username"),
    })


async def dashboard(_: web.Request):
    st = db.settings.get("stats") or {}
    return web.json_response({
        "brand": db.settings.get("brand_name", "TheDrunkBots"),
        "mongo_connected": db.connected,
        "mongo_error": db.last_error,
        "bot_running": bot.running,
        "bot_info": bot.bot_info,
        "proxy": cfg.proxy,
        "proxy_masked": mask_proxy(cfg.proxy),
        "has_proxy": bool(cfg.proxy),
        "proxy_expired": bool(db.settings.get("proxy_expired")),
        "solver_url": cfg.solver_url,
        "cache_enabled": bool(db.settings.get("cache_enabled", True)),
        "cache_hours": float(db.settings.get("cache_hours", 24)),
        "cache_count": len(db.local_cache),
        "users_count": len(db.local_users),
        "logs_count": len(db.local_logs),
        "fsub_enabled": bool(db.settings.get("force_sub_enabled", True)),
        "fsub_count": len(db.settings.get("force_sub_channels") or []),
        "maintenance_mode": bool(db.settings.get("maintenance_mode", False)),
        "stats": {
            "total_bypasses": int(st.get("total_bypasses", 0)),
            "cache_hits": int(st.get("cache_hits", 0)),
            "failed_bypasses": int(st.get("failed_bypasses", 0)),
        },
    })


async def get_settings(_: web.Request):
    await db.refresh_settings_from_mongo()
    s = dict(db.settings)
    return web.json_response({
        **s,
        "proxy_masked": mask_proxy(s.get("proxy")),
        "has_proxy": bool(s.get("proxy")),
        "bot_running": bot.running,
        "bot_info": bot.bot_info,
        "mongo_connected": db.connected,
    })


async def post_settings(req: web.Request):
    data = await req.json()
    if "proxy" in data and data.get("proxy") != cfg.proxy:
        if bypasser.busy:
            return web.json_response({"error": "A bypass job is currently running — try again in a few seconds"}, status=409)
        new_px = (data.get("proxy") or "").strip() or None
        await set_system_proxy(new_px)
        data.pop("proxy", None)

    allowed = {
        "brand_name", "admin_password", "telegram_bot_token", "telegram_api_id",
        "telegram_api_hash", "owner_id", "admin_ids", "start_photo",
        "welcome_message", "updates_channel", "force_sub_enabled",
        "force_sub_channels", "maintenance_mode", "protect_content",
        "solver_url", "solver_api_key", "accel_factor", "timeout",
        "stuck_seconds", "cache_enabled", "cache_hours", "mongo_uri",
    }
    patch = {k: v for k, v in data.items() if k in allowed}
    if patch:
        await db.update_settings(patch, notify=True)
    resp = await get_settings(req)
    if "admin_password" in patch:
        tok = make_auth_token(str(db.settings.get("admin_password") or "admin123"))
        resp.set_cookie("g2s_admin_token", tok, max_age=86400 * 30, httponly=True, samesite="Lax")
    return resp


async def proxy_test_route(_: web.Request):
    return web.json_response(await test_proxy_status())


async def create_job_route(req: web.Request):
    data = await req.json()
    urls = [u.strip() for u in str(data.get("url", "")).splitlines() if u.strip()]
    if not urls:
        return web.json_response({"error": "no url"}, status=400)
    return web.json_response({"jobs": [new_job(u, bool(data.get("force")))["id"] for u in urls]})


async def job_events_route(req: web.Request):
    job = jobs.get(req.match_info["jid"])
    if not job:
        raise web.HTTPNotFound()
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
    await resp.prepare(req)
    sent = 0
    try:
        while True:
            job["event"].clear()
            while sent < len(job["log"]):
                await resp.write(f"data: {json.dumps({'log': job['log'][sent]})}\n\n".encode())
                sent += 1
            if job["status"] == "done":
                await resp.write(f"data: {json.dumps({'done': public_job(job)})}\n\n".encode())
                break
            try:
                await asyncio.wait_for(job["event"].wait(), 15)
            except asyncio.TimeoutError:
                await resp.write(b": ping\n\n")
    except (ConnectionResetError, asyncio.CancelledError, aiohttp.ClientConnectionError):
        pass
    return resp


async def resolve_api_route(req: web.Request):
    url = req.query.get("url")
    if not url:
        return web.json_response({"error": "pass ?url="}, status=400)
    job = new_job(url, req.query.get("force") in ("1", "true"))
    await job["task"]
    if job["error"]:
        return web.json_response({"error": job["error"], "log": job["log"]}, status=502)
    return web.json_response(job["result"])


# --- Cloud Cache CRUD ---
async def list_cache_route(req: web.Request):
    q = req.query.get("q", "")
    items = await db.list_cache(search=q, limit=200)
    return web.json_response(items)


async def upsert_cache_route(req: web.Request):
    data = await req.json()
    src = (data.get("source") or "").strip()
    dest = (data.get("url") or "").strip()
    if not src or not dest:
        return web.json_response({"error": "Both source and destination url are required"}, status=400)
    entry = await db.set_cache(src, {
        "url": dest,
        "elapsed": float(data.get("elapsed") or 0.0),
        "method": data.get("method") or "admin-manual",
    })
    return web.json_response({"ok": True, "item": entry})


async def delete_cache_route(req: web.Request):
    src = req.query.get("source")
    if not src and req.can_read_body:
        try:
            body = await req.json()
            src = body.get("source")
        except Exception:
            pass
    if req.query.get("all") in ("1", "true") or not src:
        cnt = await db.clear_cache()
        return web.json_response({"ok": True, "cleared": cnt})
    ok = await db.delete_cache(src)
    return web.json_response({"ok": ok})


# --- Force-Sub CRUD ---
async def list_fsub_route(_: web.Request):
    return web.json_response({
        "enabled": bool(db.settings.get("force_sub_enabled", True)),
        "channels": db.settings.get("force_sub_channels") or [],
    })


async def save_fsub_route(req: web.Request):
    data = await req.json()
    if "enabled" in data:
        await db.update_settings({"force_sub_enabled": bool(data["enabled"])})
    if "chat_id" in data:
        cid = str(data.get("chat_id") or "").strip()
        if not cid:
            return web.json_response({"error": "chat_id required"}, status=400)
        title = (data.get("title") or cid).strip()
        invite = (data.get("invite_link") or "").strip()
        if not invite and cid.startswith("@"):
            invite = f"https://t.me/{cid.lstrip('@')}"
        channels = list(db.settings.get("force_sub_channels") or [])
        updated = False
        for c in channels:
            if str(c.get("chat_id", "")).lower() == cid.lower():
                c["chat_id"] = cid
                c["title"] = title
                c["invite_link"] = invite
                updated = True
                break
        if not updated:
            channels.append({"chat_id": cid, "title": title, "invite_link": invite})
        await db.update_settings({"force_sub_channels": channels})
    return await list_fsub_route(req)


async def delete_fsub_route(req: web.Request):
    cid = (req.query.get("chat_id") or "").strip()
    channels = [
        c for c in (db.settings.get("force_sub_channels") or [])
        if str(c.get("chat_id", "")).lower() != cid.lower()
    ]
    await db.update_settings({"force_sub_channels": channels})
    return await list_fsub_route(req)


# --- Error Logs CRUD ---
async def list_logs_route(_: web.Request):
    return web.json_response(await db.list_error_logs(limit=100))


async def delete_logs_route(req: web.Request):
    lid = (req.query.get("id") or "").strip()
    if req.query.get("all") in ("1", "true") or not lid:
        cnt = await db.clear_error_logs()
        return web.json_response({"ok": True, "cleared": cnt})
    ok = await db.delete_error_log(lid)
    return web.json_response({"ok": ok})


# --- Users & Broadcast CRUD ---
async def list_users_route(req: web.Request):
    q = req.query.get("q", "")
    return web.json_response(await db.list_users(search=q, limit=250))


async def user_ban_route(req: web.Request):
    data = await req.json()
    uid = int(data.get("user_id") or 0)
    if not uid:
        return web.json_response({"error": "user_id required"}, status=400)
    await db.set_user_banned(uid, bool(data.get("banned")))
    return web.json_response({"ok": True})


async def delete_user_route(req: web.Request):
    uid = int(req.query.get("user_id") or 0)
    if not uid:
        return web.json_response({"error": "user_id required"}, status=400)
    ok = await db.delete_user(uid)
    return web.json_response({"ok": ok})


async def broadcast_route(req: web.Request):
    data = await req.json()
    text = (data.get("message") or "").strip()
    if not text:
        return web.json_response({"error": "message is empty"}, status=400)
    if not bot.running:
        return web.json_response({"error": "Telegram Bot is not running (set telegram_bot_token first)"}, status=400)
    uids = await db.get_all_user_ids(include_banned=False)
    sent_ok = 0
    failed = 0
    formatted = f"📢 • <b>𝗔𝗡𝗡𝗢𝗨𝗡𝗖𝗘𝗠𝗘𝗡𝗧</b>\n━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>{text}</blockquote>"
    for uid in uids:
        r = await bot.send_message(uid, formatted)
        if r and r.get("ok"):
            sent_ok += 1
        else:
            failed += 1
        await asyncio.sleep(0.04)
    return web.json_response({"ok": True, "delivered": sent_ok, "failed": failed, "total": len(uids)})


# --------------------------------------------------------- Background Monitors
async def _background_sync_and_tunnel_monitor():
    while True:
        await asyncio.sleep(45)
        try:
            await db.refresh_settings_from_mongo()
            _apply_db_settings_to_cfg()
            await bot.reload_if_needed()
            active_px = (db.settings.get("proxy") or "").strip()
            if active_px and not db.settings.get("proxy_expired") and not bypasser.busy:
                info = await test_proxy_status(active_px)
                if not info.get("ok"):
                    await bot.notify_tunnel_expired(active_px, info.get("error") or "Tunnel unreachable")
        except asyncio.CancelledError:
            break
        except Exception:
            pass


async def on_startup(app: web.Application):
    await db.connect()
    _apply_db_settings_to_cfg()
    bypasser._ensure_display()
    print(f"Starting browser (headless={cfg.headless}, channel={cfg.browser_channel!r})…", flush=True)
    if cfg.proxy:
        print(f"Proxy configured: {mask_proxy(cfg.proxy)}", flush=True)
    else:
        print("Proxy: none configured at startup (can be set via Web Admin Panel or Telegram Bot).", flush=True)
    await bypasser.start()
    await bot.start()
    app["monitor_task"] = asyncio.create_task(_background_sync_and_tunnel_monitor())
    url = f"http://{HOST}:{PORT}/"
    print(f"\n  🍹 TheDrunkBots Admin Panel & API ready → {url}\n", flush=True)
    if not NO_OPEN:
        try:
            webbrowser.open(url)
        except Exception:
            pass


async def on_cleanup(app: web.Application):
    t = app.get("monitor_task")
    if t:
        t.cancel()
    await bot.stop()
    await bypasser.close()


app = web.Application(middlewares=[auth_middleware])
app.router.add_get("/", index)
app.router.add_post("/api/auth/login", auth_login)
app.router.add_post("/api/auth/logout", auth_logout)
app.router.add_get("/api/auth/me", auth_me)
app.router.add_get("/api/health", health)
app.router.add_get("/api/dashboard", dashboard)
app.router.add_get("/api/settings", get_settings)
app.router.add_post("/api/settings", post_settings)
app.router.add_get("/api/proxy-test", proxy_test_route)
app.router.add_post("/api/jobs", create_job_route)
app.router.add_get("/api/jobs/{jid}/events", job_events_route)
app.router.add_get("/api/resolve", resolve_api_route)
app.router.add_get("/api/cache", list_cache_route)
app.router.add_post("/api/cache", upsert_cache_route)
app.router.add_delete("/api/cache", delete_cache_route)
app.router.add_get("/api/history", list_cache_route)
app.router.add_delete("/api/history", delete_cache_route)
app.router.add_get("/api/fsub", list_fsub_route)
app.router.add_post("/api/fsub", save_fsub_route)
app.router.add_delete("/api/fsub", delete_fsub_route)
app.router.add_get("/api/logs", list_logs_route)
app.router.add_delete("/api/logs", delete_logs_route)
app.router.add_get("/api/users", list_users_route)
app.router.add_post("/api/users/ban", user_ban_route)
app.router.add_delete("/api/users", delete_user_route)
app.router.add_post("/api/broadcast", broadcast_route)
app.on_startup.append(on_startup)
app.on_cleanup.append(on_cleanup)

if __name__ == "__main__":
    print(f"Listening on {HOST}:{PORT}", flush=True)
    web.run_app(app, host=HOST, port=PORT, print=None)
