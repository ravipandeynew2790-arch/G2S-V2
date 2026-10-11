"""
get2short bypass engine.

Drives a real (hidden) Chrome through the whole shortener chain automatically:

  get2short.com/<alias>          browser check (3 s, server enforced)
  -> safe.php (CF challenge)     auto-passes in a real browser on a home IP
  -> blog step pages             timers accelerated, "click on ads" faked,
                                 turnstile solved via solver, buttons clicked
  -> link.get2short.com/<alias>?wp=...   final page: turnstile + /links/go
  -> destination URL             captured from the /links/go JSON response
                                 (the destination page itself is never loaded)

Whenever a page already contains the final ?wp= link in its source, the engine
jumps straight to it and skips the remaining blog steps.
"""
import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

try:  # patchright = Playwright patched against bot detection (preferred)
    from patchright.async_api import async_playwright
    LIB = "patchright"
except ImportError:  # pragma: no cover
    from playwright.async_api import async_playwright
    LIB = "playwright"

from .scripts import (CLOSE_OVERLAYS_JS, INIT_SCRIPT, INJECT_TOKEN_JS, JS_CLICK,
                      SCAN_JS, SUBMIT_FORM_JS, SUBMIT_GO_FORM_JS)
from .solver import TurnstileSolver

# (regex, score) - higher score wins. Matched against visible button/link text.
PATTERNS = [
    [r"^get\s*link\b", 100],
    [r"^continue\b|^proceed\b", 90],
    [r"click here to (next|continue|proceed)|^next step$|\bgo to (link|destination)\b|dual tap|click open", 80],
    [r"^verify(\s+(now|human|link|to continue|you are human))?$|^i'?m not a robot|^i am human|^click to verify$", 70],
    [r"^(click here|get|generate( link)?|open( link)?|go|get download link|skip( ad)?|unlock|"
     r"show link|reveal link|get your link|download link)$", 50],
]
EXCLUDE = (r"wait|please|\d+\s*s(ec|econds)?\b|verifying|telegram|youtube|instagram|whatsapp|"
           r"facebook|twitter|join|start download|got it|privacy|terms|about|contact|disclaimer|"
           r"learn more|cookie|log ?in|sign ?(in|up)|register|\bhome\b|menu|search|share|comment|"
           r"reply|subscribe|install|\bapp\b|\bapk\b")
BLOCK_RE = re.compile(r"ip_risk_blocked|vpn detected|turn off your vpn|access temporarily restricted|"
                      r"browser not supported", re.I)
CF_HOST = "challenges.cloudflare.com"


@dataclass
class Config:
    solver_url: str = "https://cf.vltcx.eu.cc"
    solver_api_key: str | None = None
    browser_channel: str | None = "chrome"     # "chrome", "msedge" or None (bundled chromium)
    headless: bool = False                     # headless is often refused by Cloudflare
    show_browser: bool = False                 # False = real window parked off-screen
    accel_factor: float = 0.1                  # client side timers run 10x faster
    no_accel_hosts: list = field(default_factory=lambda: [
        "get2short.com", "liteshort.com", "nowshort.com", "linksflys.com",
        "vplink.in", "arolinks.com", "babylinks.in", "alpha-links.in", "urllinkshort.in",
        "lksfy.com", "softurl.in", "shortwala.com", "earnlinks.in", "inshorturl.in", "inshorturl.com"
    ])
    timeout: int = 300
    stuck_seconds: int = 100                   # give up if one page doesn't progress
    native_turnstile_grace: float = 4.0        # let the widget auto-pass before using solver
    max_solver_calls: int = 6
    profile_dir: str = "profile"
    debug_dir: str = "debug"
    debug: bool = False
    block_heavy: bool = False                  # request interception; breaks Cloudflare checks, keep off
    extra_args: list = field(default_factory=list)
    proxy: str | None = None                   # http://user:pass@host:port (residential/home)
    timezone: str | None = "auto"              # "auto" = match the exit IP, or e.g. "Asia/Kolkata"
    cf_patience: int = 90                      # seconds to wait for a Cloudflare interstitial

    @classmethod
    def load(cls, path: str) -> "Config":
        c = cls()
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for k, v in json.load(f).items():
                    if hasattr(c, k):
                        setattr(c, k, v)
        return c


class BypassError(Exception):
    pass


def host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def base_domain(host: str) -> str:
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def parse_proxy(url: str | None):
    """'http://user:pass@host:port' -> Playwright proxy dict."""
    if not url:
        return None
    url = url.strip()
    if "://" not in url:
        url = "http://" + url
    u = urlparse(url)
    d = {"server": f"{u.scheme}://{u.hostname}:{u.port or (1080 if u.scheme.startswith('socks') else 80)}"}
    if u.username:
        from urllib.parse import unquote
        d["username"] = unquote(u.username)
        d["password"] = unquote(u.password or "")
    return d


def decode_timer_token(tok: str | None):
    """ak_safelink_timer_token = base64('issued_ts|seconds|hash|nonce|hmac') -> ready epoch."""
    if not tok:
        return None
    try:
        import base64
        raw = base64.b64decode(tok + "=" * (-len(tok) % 4)).decode("utf-8", "ignore")
        parts = raw.split("|")
        ts, secs = int(parts[0]), int(parts[1])
        if 1_000_000_000 < ts < 4_000_000_000 and 0 <= secs < 600:
            return ts + secs
    except Exception:
        pass
    return None


def short(url: str, n: int = 90) -> str:
    return url if len(url) <= n else url[:n] + "…"


class Job:
    def __init__(self, url: str, log, loop):
        self.start_url = url
        self.base = base_domain(host_of(url))
        self.log_cb = log
        self.result = loop.create_future()
        self.t0 = time.monotonic()
        self.method = None
        self.reached_final = False
        self.chain_hosts = {host_of(url)}
        if self.base == "inshorturl.in":
            self.chain_hosts.add("inshorturl.com")
        elif self.base == "inshorturl.com":
            self.chain_hosts.add("inshorturl.in")
        self.steps = []
        self.cur_url = None
        self.url_since = time.monotonic()
        self.clicks = {}
        self.ts_first = {}
        self.solver_tasks = {}
        self.injected = set()
        self.solver_calls = 0
        self.ad_attempts = {}
        self.last_ad_end = 0.0
        self.challenge_click_at = 0.0
        self.go_error = None
        self.go_error_at = 0.0
        self.go_resubmits = 0
        self.start_visits = 0
        self.switch_to = None
        self.blocked_since = None
        self.reload_at = 0.0
        self._last_msg = None
        self.debug_n = 0
        self.ts_logged = set()
        self.fatal = None
        self.form_submits = {}
        self.challenge_since = None
        self.timer_logged = set()
        self.shortcut = None
        self.shortcut_used = set()
        self.pending_redirect = None
        self.pending_redirect_used = set()
        self.tried_tokens = set()

    def check_shortcut(self, u: str):
        if not u or not u.startswith("http"):
            return
        try:
            from urllib.parse import parse_qs, unquote
            p = urlparse(u)
            h = (p.hostname or "").lower()
            if not h or h == CF_HOST:
                return
            q = parse_qs(p.query)
            if p.path.rstrip("/") == "/st" and "api" in q and "url" in q:
                inner = unquote(q["url"][0]).strip()
                ih = host_of(inner)
                if ih and (ih == self.base or ih.endswith("." + self.base)) and base_domain(h) != self.base:
                    self.shortcut = (inner, f"https://link.{base_domain(h)}/")
                    return
            if "sid" in q:
                sid = unquote(q["sid"][0]).strip()
                sh = host_of(sid)
                if sh and (sh == self.base or sh.endswith("." + self.base)) and base_domain(h) != self.base:
                    self.shortcut = (sid, f"https://{h}/")
                    return
        except Exception:
            pass

    def log(self, msg):
        if msg != self._last_msg:
            self._last_msg = msg
            self.log_cb(msg)

    def set_result(self, url, method):
        if not self.result.done():
            self.method = method
            self.result.set_result(url)

    def is_chain(self, url):
        h = host_of(url)
        if "antibypass" in h or h.endswith("prostore7.pages.dev"):
            return True
        return h in self.chain_hosts or h == self.base or h.endswith("." + self.base)

    def is_dest(self, url):
        if not url.lower().startswith(("http://", "https://")):
            return False
        h = host_of(url)
        return bool(h) and not self.is_chain(url) and h != CF_HOST


def ts_pending_check(snap):
    ts = snap.get("turnstile") or {}
    return bool(ts.get("present") and not ts.get("solved"))


class Bypasser:
    def __init__(self, config: Config):
        self.cfg = config
        self.solver = TurnstileSolver(config.solver_url, config.solver_api_key)
        self._pw = None
        self.ctx = None
        self._accel = config.accel_factor
        self._lock = asyncio.Lock()
        self.current_page = None
        self.busy = False
        self._xvfb_proc = None
        self.on_proxy_expired = None

    def _ensure_display(self):
        if os.name == "nt":
            return
        force_headless = os.environ.get("G2S_FORCE_HEADLESS", "").strip().lower() in ("1", "true", "yes")
        if force_headless:
            self.cfg.headless = True
            return
        if os.environ.get("DISPLAY"):
            self.cfg.headless = False
            return
        import shutil
        import subprocess
        xvfb = shutil.which("Xvfb")
        if xvfb:
            for disp_num in range(99, 125):
                disp = f":{disp_num}"
                lock = f"/tmp/.X{disp_num}-lock"
                if not os.path.exists(lock):
                    try:
                        self._xvfb_proc = subprocess.Popen(
                            [xvfb, disp, "-screen", "0", "1280x860x24", "-ac", "-nolisten", "tcp"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        )
                        os.environ["DISPLAY"] = disp
                        self.cfg.headless = False
                        time.sleep(0.3)
                        return
                    except Exception:
                        pass
        self.cfg.headless = True

    async def set_proxy(self, proxy: str | None):
        async with self._lock:
            self.cfg.proxy = proxy or None
            if self.ctx is not None:
                await self.ctx.close()
                self.ctx = None
            await self.start()

    # ------------------------------------------------------------------ browser
    async def start(self, accel: float | None = None):
        if accel is not None:
            self._accel = accel
        self._ensure_display()
        if self._pw is None:
            self._pw = await async_playwright().start()
        c = self.cfg
        args = ["--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                "--disable-features=CalculateNativeWinOcclusion",
                "--no-first-run", "--no-default-browser-check",
                "--window-size=1280,860"]
        if not c.headless and not c.show_browser:
            args.append("--window-position=-2400,-2400")
        kw = {}
        if c.headless:
            kw["user_agent"] = (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            )
        if LIB == "playwright":
            args.append("--disable-blink-features=AutomationControlled")
            kw["ignore_default_args"] = ["--enable-automation"]
        args += list(c.extra_args)
        os.makedirs(c.profile_dir, exist_ok=True)
        for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            lock_path = os.path.join(c.profile_dir, lock_name)
            try:
                if os.path.lexists(lock_path):
                    os.remove(lock_path)
            except Exception:
                pass
        px = parse_proxy(c.proxy)
        if px:
            kw["proxy"] = px
        tz = await self._timezone()
        if tz:
            kw["timezone_id"] = tz
        channels = [c.browser_channel, None] if c.browser_channel else [None]
        last = None
        for ch in channels:
            try:
                self.ctx = await self._pw.chromium.launch_persistent_context(
                    os.path.abspath(c.profile_dir), channel=ch, headless=c.headless,
                    no_viewport=True, args=args, **kw)
                break
            except Exception as e:  # channel not installed -> bundled chromium
                last = e
        else:
            raise BypassError(f"Could not start browser: {last}")
        script = (INIT_SCRIPT.replace("__FACTOR__", str(float(self._accel)))
                  .replace("__NOACCEL__", json.dumps(c.no_accel_hosts)))
        await self.ctx.add_init_script(script)
        # keep one blank tab forever - a headed browser exits when its last tab closes
        self._keeper = self.ctx.pages[0] if self.ctx.pages else await self.ctx.new_page()
        for p in list(self.ctx.pages):
            if p is not self._keeper:
                try:
                    await p.close()
                except Exception:
                    pass

    async def _timezone(self):
        """Browser timezone must match the exit IP or Cloudflare gets suspicious."""
        tz = self.cfg.timezone
        if tz and tz != "auto":
            return tz
        if not self.cfg.proxy:
            return None  # direct connection: system timezone already matches
        try:
            import aiohttp
            d = parse_proxy(self.cfg.proxy)
            kw = {"proxy": d["server"]}
            if d.get("username"):
                kw["proxy_headers"] = {"Proxy-Authorization": "Basic " + __import__("base64").b64encode(
                    f"{d['username']}:{d.get('password', '')}".encode()).decode()}
            async with aiohttp.ClientSession() as s:
                async with s.get("https://ipinfo.io/json", timeout=aiohttp.ClientTimeout(total=15), **kw) as r:
                    return (await r.json(content_type=None)).get("timezone")
        except Exception:
            return None

    async def close(self):
        try:
            if self.ctx:
                await self.ctx.close()
        except Exception:
            pass
        self.ctx = None
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self._pw = None
        if getattr(self, "_xvfb_proc", None) is not None:
            try:
                self._xvfb_proc.terminate()
            except Exception:
                pass
            self._xvfb_proc = None

    async def _restart(self, accel):
        if self.ctx:
            await self.ctx.close()
            self.ctx = None
        await self.start(accel)

    async def _eval(self, page, js, arg=None):
        """Evaluate in the page's main world (needed to reach page globals)."""
        if LIB == "patchright":
            return await page.evaluate(js, arg, isolated_context=False)
        return await page.evaluate(js, arg)

    # --------------------------------------------------------------- public API
    async def resolve(self, url: str, log=print) -> dict:
        url = url.strip()
        if not re.match(r"^https?://", url):
            url = "https://" + url
        async with self._lock:
            if self.ctx is None:
                await self.start()
            t0 = time.monotonic()
            try:
                return await self._attempt(url, log, t0)
            except BypassError as e:
                if self.cfg.proxy and any(err in str(e) for err in (
                    "ERR_PROXY_CONNECTION_FAILED", "ERR_TUNNEL_CONNECTION_FAILED",
                    "ERR_CONNECTION_RESET", "ERR_EMPTY_RESPONSE", "ERR_CONNECTION_CLOSED"
                )):
                    expired_px = self.cfg.proxy
                    log("⚠ Proxy connection failed (tunnel offline/expired) — switching to direct connection…")
                    self.cfg.proxy = None
                    if self.on_proxy_expired:
                        try:
                            res_cb = self.on_proxy_expired(expired_px, str(e))
                            if asyncio.iscoroutine(res_cb):
                                asyncio.ensure_future(res_cb)
                        except Exception:
                            pass
                    await self._restart(self._accel)
                    t0 = time.monotonic()
                    return await self._attempt(url, log, t0)
                if self._accel >= 1 or "blocked" in str(e).lower() or "server redirect loop" in str(e).lower():
                    raise
                log(f"⚠ {e} — retrying once with real-time timers (no acceleration)…")
                normal = self._accel
                await self._restart(1.0)
                try:
                    return await self._attempt(url, log, t0)
                finally:
                    await self._restart(normal)

    # ------------------------------------------------------------------ engine
    async def _attempt(self, url, log, t0):
        loop = asyncio.get_running_loop()
        job = Job(url, log, loop)
        try:
            page = await self.ctx.new_page()
        except Exception:  # browser died (closed window, crash) -> relaunch
            job.log("Browser was closed — restarting it")
            try:
                await self.ctx.close()
            except Exception:
                pass
            self.ctx = None
            await self.start()
            page = await self.ctx.new_page()
        self.current_page = page
        self.busy = True
        if job.base == "arolinks.com":
            try:
                await self.ctx.clear_cookies(domain=re.compile(r"(^|\.)(arolinks\.com|techmint\.in|bcsakhi\.in)$"))
            except Exception:
                pass
        await self._attach(job, page)
        job.log(f"Opening {url}")
        try:
            try:
                await page.goto(url, wait_until="commit", timeout=45000)
            except Exception as e:
                raise BypassError(f"Could not open link: {e}")
            deadline = time.monotonic() + self.cfg.timeout
            while not job.result.done():
                if time.monotonic() > deadline:
                    raise BypassError("Timed out")
                if job.switch_to is not None:
                    old, page, job.switch_to = page, job.switch_to, None
                    self.current_page = page
                    await self._attach(job, page, init=False)
                    try:
                        await old.close()
                    except Exception:
                        pass
                    job.log("Continuing in the tab opened by the page")
                page = await self._tick(job, page)
                if not job.result.done():
                    await asyncio.sleep(0.35)
            dest = job.result.result()
            elapsed = round(time.monotonic() - t0, 1)
            job.log(f"✔ Destination found in {elapsed}s ({job.method})")
            return {"url": dest, "elapsed": elapsed, "method": job.method,
                    "steps": job.steps, "source": url}
        except BypassError:
            await self._dump(job, page, "FAIL")
            raise
        finally:
            self.busy = False
            self.current_page = None
            for t in job.solver_tasks.values():
                t.cancel()
            for p in list(self.ctx.pages) if self.ctx else []:
                if p is getattr(self, "_keeper", None):
                    continue
                try:
                    await p.close()
                except Exception:
                    pass

    async def _unwrap_antibypass_track(self, job, track_url: str, referer_url: str) -> bool:
        try:
            import aiohttp
            from urllib.parse import urljoin
            ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
            p_ref = urlparse(referer_url)
            ref = f"{p_ref.scheme}://{p_ref.hostname}/" if p_ref.hostname else referer_url
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
                async with s.get(
                    track_url,
                    headers={"User-Agent": ua, "Referer": ref},
                    allow_redirects=False,
                ) as r:
                    loc = r.headers.get("Location") or r.headers.get("location")
                    if not loc:
                        return False
                    full_loc = urljoin(track_url, loc)
            if "linkId=" not in full_loc or "accessToken=" not in full_loc:
                return False
            job.log("Unwrapping antibypass gate via solver…")
            tok = await self.solver.solve_turnstile("0x4AAAAAAEQs2qPASaTqNrwD", full_loc)
            return await self._verify_antibypass(job, full_loc, tok)
        except Exception:
            return False

    async def _verify_antibypass(self, job, page_url: str, token: str) -> bool:
        try:
            from urllib.parse import parse_qs
            p = urlparse(page_url)
            q = parse_qs(p.query)
            link_id = (q.get("linkId") or [None])[0]
            access_token = (q.get("accessToken") or [None])[0]
            if not link_id or not access_token or not token:
                return False
            import aiohttp
            ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
            origin = f"{p.scheme}://{p.hostname}"
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
                async with s.post(
                    f"https://antibypass-bot.prostore7.workers.dev/verify/{link_id}",
                    json={"token": token, "accessToken": access_token},
                    headers={"User-Agent": ua, "Origin": origin, "Referer": origin + "/"},
                ) as r:
                    data = await r.json(content_type=None)
                    if isinstance(data, dict) and data.get("success") and data.get("originalUrl"):
                        job.set_result(data["originalUrl"], "antibypass verify API")
                        return True
        except Exception:
            pass
        return False

    async def _attach(self, job, page, init=True):
        cfg = self.cfg
        try:
            await page.add_init_script(f"window.__g2sBaseHost = {json.dumps(job.base)};")
        except Exception:
            pass

        async def route_handler(route, request):
            try:
                u = request.url
                if (job.reached_final and request.is_navigation_request()
                        and request.frame == page.main_frame and job.is_dest(u)):
                    job.set_result(u, "redirect")
                    await route.abort()
                    return
                if (cfg.block_heavy and request.resource_type in ("image", "media", "font")
                        and CF_HOST not in u):
                    await route.abort()
                    return
                await route.continue_()
            except Exception:
                pass

        async def on_response(resp):
            try:
                u = resp.url
                job.check_shortcut(u)
                if resp.status in (301, 302, 303, 307, 308):
                    loc = resp.headers.get("location")
                    if loc:
                        from urllib.parse import urljoin
                        full_loc = urljoin(u, loc)
                        job.check_shortcut(full_loc)
                        if (job.reached_final or "redirect.php" in u) and job.is_dest(full_loc):
                            job.set_result(full_loc, "redirect")
                            return
                if "protection-browser-check" in u:
                    data = await resp.json()
                    reason = data.get("reason")
                    if data.get("status") != "success" and reason in ("ip_risk_blocked", "vpn_blocked",
                                                                      "brave_blocked"):
                        job.fatal = (f"get2short blocked this IP/browser ({reason}). Use a home/mobile "
                                     "connection (or set a residential proxy) with VPN off.")
                    return
                if "/verify/" in u and resp.request.method == "POST":
                    data = await resp.json()
                    if data.get("success") and data.get("originalUrl"):
                        job.set_result(data["originalUrl"], "antibypass verify API")
                    return
                if "/links/go" in u and resp.request.method == "POST":
                    data = await resp.json()
                    if data.get("status") == "success" and data.get("url"):
                        dest_url = data["url"]
                        if job.is_dest(dest_url):
                            job.set_result(dest_url, "links/go API")
                        elif dest_url.split("#")[0].rstrip("/") == job.start_url.split("#")[0].rstrip("/"):
                            job.go_error = "timer not ready"
                            job.go_error_at = time.monotonic()
                            job.log("Final API returned start URL (timer not ready) — waiting to retry")
                        elif dest_url.lower().startswith(("http://", "https://")):
                            job.reached_final = True
                            job.pending_redirect = dest_url
                        else:
                            job.reached_final = True
                    else:
                        job.go_error = data.get("message") or str(data)[:200]
                        job.go_error_at = time.monotonic()
                        job.log(f"Final API said: {job.go_error}")
            except Exception:
                pass

        async def on_popup(p):
            try:
                for _ in range(25):
                    if p.url and p.url != "about:blank":
                        break
                    await asyncio.sleep(0.2)
                u = p.url
                if job.reached_final and job.is_dest(u):
                    job.set_result(u, "new tab")
                elif "wp=" in u or (job.is_chain(u) and u != "about:blank"):
                    job.switch_to = p
                    return
                await p.close()
            except Exception:
                pass

        def on_nav(frame):
            if frame != page.main_frame:
                return
            u = frame.url
            job.check_shortcut(u)
            if job.reached_final and job.is_dest(u):
                job.set_result(u, "redirect")
            elif not job.reached_final and u.startswith("http") and host_of(u) != CF_HOST:
                job.chain_hosts.add(host_of(u))

        def on_download(d):
            if job.reached_final:
                job.set_result(d.url, "download")
            asyncio.ensure_future(d.cancel())

        if cfg.block_heavy:
            await page.route("**/*", route_handler)
        page.on("response", lambda r: asyncio.ensure_future(on_response(r)))
        page.on("popup", lambda p: asyncio.ensure_future(on_popup(p)))
        page.on("framenavigated", on_nav)
        page.on("download", on_download)
        page.on("dialog", lambda d: asyncio.ensure_future(d.dismiss()))

    async def _scan(self, job, page):
        try:
            return await self._eval(page, SCAN_JS,
                                    {"base": job.base, "patterns": PATTERNS, "exclude": EXCLUDE})
        except Exception:
            return None  # navigating / page not ready

    async def _dump(self, job, page, tag):
        try:
            d = os.path.join(self.cfg.debug_dir, time.strftime("%Y%m%d-%H%M%S"))
            os.makedirs(d, exist_ok=True)
            job.debug_n += 1
            name = f"{job.debug_n:02d}_{tag}_{host_of(page.url)}"
            with open(os.path.join(d, name + ".html"), "w", encoding="utf-8") as f:
                f.write(f"<!-- {page.url} -->\n" + await page.content())
            await page.screenshot(path=os.path.join(d, name + ".png"), full_page=True, timeout=8000)
            job.log(f"Saved debug snapshot: {os.path.join(d, name)}.html/.png")
        except Exception:
            pass

    async def _tick(self, job, page):
        cfg = self.cfg
        now = time.monotonic()
        # Cloudflare interstitial: touch NOTHING in the page (no main-world JS, no snapshots)
        try:
            title = await page.title()
        except Exception:
            return page
        if re.match(r"^(just a moment|attention required|one more step)", title, re.I):
            host = host_of(page.url)
            job.challenge_since = job.challenge_since or now
            job.url_since = now
            if now - job.challenge_since > cfg.cf_patience:
                raise BypassError(f"Cloudflare keeps re-challenging on {host} — this IP is blocked "
                                  "by Cloudflare. Use a home/mobile IP or residential proxy.")
            job.log(f"Cloudflare check on {host}… waiting (passes automatically on a home IP)")
            if now - job.challenge_since > 25 and now - job.challenge_click_at > 12:
                job.challenge_click_at = now
                if await self._click_cf_checkbox(page):
                    job.log("Clicked the Cloudflare checkbox")
            await asyncio.sleep(1.0)
            return page
        snap = await self._scan(job, page)
        if snap is None:
            return page
        url = snap["url"]
        host = host_of(url)
        if job.fatal:
            raise BypassError(job.fatal)

        if job.pending_redirect and job.pending_redirect not in job.pending_redirect_used:
            target = job.pending_redirect
            job.pending_redirect_used.add(target)
            if "antibypass" in target and "/track/" in target:
                if await self._unwrap_antibypass_track(job, target, url):
                    return page
            job.log(f"Following final redirect → {short(target)}")
            try:
                await page.goto(target, wait_until="commit", timeout=30000, referer=url)
            except Exception:
                pass
            return page

        if job.shortcut and not job.reached_final:
            target_url, ref_url = job.shortcut
            if target_url not in job.shortcut_used and host and host != host_of(job.start_url):
                job.shortcut_used.add(target_url)
                job.log(f"⚡ Shortcut found in redirect chain → {short(target_url)}")
                try:
                    await page.goto(target_url, wait_until="domcontentloaded", timeout=45000, referer=ref_url)
                except Exception:
                    pass
                return page

        # --- transient proxy / network error page recovery ---------------------
        if url.startswith("chrome-error://") or host == "chromewebdata":
            if now - job.reload_at > 4:
                job.reload_at = now
                retry_url = next((u for u in reversed(job.steps) if u.startswith("http")), job.start_url)
                job.log(f"Network error page — retrying {short(retry_url, 60)}")
                try:
                    await asyncio.sleep(1.5)
                    await page.goto(retry_url, wait_until="commit")
                except Exception:
                    pass
            return page

        # --- new page -----------------------------------------------------------
        if url != job.cur_url:
            job.cur_url, job.url_since = url, now
            if host != CF_HOST and url.startswith("http") and "__cf_chl" not in url:
                job.steps.append(url)
                job.log(f"→ {short(url)}")
                if url.split("#")[0].rstrip("/") == job.start_url.rstrip("/"):
                    job.start_visits += 1
                    if job.start_visits > 3:
                        raise BypassError("Shortener keeps sending us back to the start")
                if cfg.debug:
                    await self._dump(job, page, "step")
        if now - job.url_since > cfg.stuck_seconds:
            raise BypassError(f"Stuck on {short(url)} for {cfg.stuck_seconds}s")

        text = snap.get("text", "")

        # --- hard blocks (VPN / datacenter IP / unsupported browser) -----------
        if BLOCK_RE.search(text) or BLOCK_RE.search(snap.get("title", "")):
            job.blocked_since = job.blocked_since or now
            if now - job.blocked_since > 4:
                raise BypassError("Blocked by the site (VPN/datacenter IP or browser refused). "
                                  "Run on a normal home/mobile connection with VPN off.")
        else:
            job.blocked_since = None
        if re.search(r"verification failed|please refresh and try again", text, re.I) \
                and now - job.reload_at > 10:
            job.reload_at = now
            job.log("Browser check failed — reloading")
            try:
                await page.reload(wait_until="commit")
            except Exception:
                pass
            return page

        # --- final page detection --------------------------------------------------
        on_base = host == job.base or host.endswith("." + job.base)
        if job.reached_final and not on_base and host in job.chain_hosts and "prostore7.pages.dev" not in host:
            job.reached_final = False
        if on_base and ("wp=" in url or snap.get("finalMarkers")) and not job.reached_final:
            job.reached_final = True
            job.log("Reached the shortener's final page")
        if job.reached_final:
            h = snap.get("getLinkHref") or ""
            if job.is_dest(h) and not snap.get("getLinkDisabled"):
                job.set_result(h, "Get Link button")
                return page

        # --- Cloudflare interstitial ("Just a moment…") --------------------------
        if snap.get("challenge"):
            job.challenge_since = job.challenge_since or now
            job.url_since = now  # CF reloads itself; don't count as "stuck"
            if now - job.challenge_since > cfg.cf_patience:
                raise BypassError(f"Cloudflare keeps re-challenging on {host} — this IP is blocked "
                                  "by Cloudflare. Use a home/mobile IP or residential proxy.")
            job.log(f"Cloudflare check on {host}… (passes automatically on a home IP)")
            if now - job.challenge_since > 25 and now - job.challenge_click_at > 12:
                job.challenge_click_at = now
                await self._click_cf_checkbox(page)
            return page

        job.challenge_since = None

        # --- "find us on Google" step: skip Google, open the blog with a Google referrer --
        if re.match(r"(www\.)?google\.[a-z.]+$", host):
            from urllib.parse import parse_qs, unquote
            q = parse_qs(urlparse(url).query)
            direct_u = (q.get("url") or [""])[0].strip()
            if direct_u and direct_u.startswith(("http://", "https://")):
                job.log(f"⚡ Google redirect unwrapped → {short(direct_u)}")
                try:
                    await page.goto(direct_u, wait_until="commit", referer="https://www.google.com/")
                except Exception:
                    pass
                return page
            inner = q.get("continue", [""])[0]
            if inner:
                q = parse_qs(urlparse(unquote(inner)).query)
            term = (q.get("q", [""])[0] or "").strip()
            m = re.search(r"([a-z0-9-]+(\.[a-z0-9-]+)+)", term, re.I)
            if m:
                target = "https://" + m.group(1).lower() + "/"
                job.log(f"⚡ Google-search step skipped → opening {target} with Google referrer")
                try:
                    await page.goto(target, wait_until="commit", referer="https://www.google.com/")
                except Exception:
                    pass
                return page

        # --- newwpsafelink (e.g. mtc1.jobsraavi.in / softurl.in) ---------------------
        nwp = snap.get("newWpSafelink")
        if nwp and not job.reached_final:
            target = nwp.get("url")
            delay = float(nwp.get("delay") or 25) + 1.0
            if target and target not in job.shortcut_used:
                elapsed = time.monotonic() - job.t0
                if elapsed < delay:
                    wait_left = delay - elapsed
                    job.log(f"Safelink token found — waiting {wait_left:.0f}s for server timer ({delay:.0f}s required)…")
                    while time.monotonic() - job.t0 < delay:
                        job.url_since = time.monotonic()
                        await asyncio.sleep(min(1.5, delay - (time.monotonic() - job.t0)))
                job.shortcut_used.add(target)
                job.log(f"⚡ Jumping to safelink destination → {short(target)}")
                try:
                    await page.goto(target, wait_until="commit", referer=url)
                except Exception:
                    pass
                return page

        # --- shortcut: final ?wp= or final subdomain link already present in the page source --------
        if snap.get("wpLinks") and not job.reached_final:
            target = snap["wpLinks"][0]
            job.log("⚡ Final link found in page source — skipping remaining steps")
            try:
                await page.goto(target, wait_until="commit", referer=url)
            except Exception:
                pass
            return page

        # --- WP safelink form: server only checks the timer token -> wait & submit ------
        forms = snap.get("safeForms") or []
        total_forms = sum(c for c, _ in job.form_submits.values())
        if forms and not job.reached_final and total_forms < 8:
            f = forms[0]
            if f["id"].endswith("_any1") and any(k[1].endswith("_get") for k in job.form_submits):
                raise BypassError(
                    f"{job.base} server redirect loop: redirected back to {host} Step 1 after completing all 4 steps"
                )
            ready = decode_timer_token(f.get("token"))
            key = (url, f["id"])
            cnt, last = job.form_submits.get(key, (0, 0.0))
            if ts_pending_check(snap):
                pass  # let turnstile logic below run first
            elif ready is not None and time.time() < ready + 1.0:
                left = ready + 1.0 - time.time()
                if url not in job.timer_logged:
                    job.timer_logged.add(url)
                    job.log(f"Safelink form found — server timer {left:.0f}s left, then auto-submit "
                            "(skipping ads/buttons on this page)")
                await asyncio.sleep(min(left, 1.0))
                return page
            elif cnt < 3 and now - last > 8:
                job.form_submits[key] = (cnt + 1, now)
                job.log(f"Submitting safelink form → {short(f.get('action') or '', 70)}")
                try:
                    await self._eval(page, SUBMIT_FORM_JS, f["id"])
                except Exception:
                    pass
                await asyncio.sleep(1.0)
                return page
            elif cnt < 3:
                await asyncio.sleep(0.5)
                return page

        # --- turnstile widgets -----------------------------------------------------
        ts = snap.get("turnstile") or {}
        is_antibypass = "prostore7.pages.dev" in host or ("linkId=" in url and "accessToken=" in url)
        tok_now = ts.get("token")
        if is_antibypass and tok_now and tok_now not in job.tried_tokens:
            job.tried_tokens.add(tok_now)
            if await self._verify_antibypass(job, url, tok_now):
                return page
        ts_pending = ts.get("present") and (not ts.get("solved") or is_antibypass)
        ts_pending_override = False
        for key, task in list(job.solver_tasks.items()):
            if task.done() and key not in job.injected:
                job.injected.add(key)
                if key[0] != url:
                    continue
                try:
                    token = task.result()
                    if is_antibypass and await self._verify_antibypass(job, url, token):
                        return page
                    n = await self._eval(page, INJECT_TOKEN_JS, token)
                    job.log(f"Turnstile solved by solver, token injected ({n} targets)")
                    ts_pending_override = True
                except Exception as e:
                    job.log(f"Solver failed: {e}")
                    job.ts_first[url] = now  # allow another attempt later
        if ts_pending_override:
            return page  # re-scan with the token in place
        if ts_pending:
            first = job.ts_first.setdefault(url, now)
            keys = ts.get("sitekeys") or []
            grace = 0.0 if is_antibypass else cfg.native_turnstile_grace
            if keys and now - first >= grace:
                key = (url, keys[0])
                running = key in job.solver_tasks and not job.solver_tasks[key].done()
                if not running and job.solver_calls < cfg.max_solver_calls and \
                        (key not in job.solver_tasks or key in job.injected):
                    job.solver_calls += 1
                    job.injected.discard(key)
                    job.log(f"Turnstile not auto-passed — asking solver (sitekey {keys[0][:14]}…)")
                    job.solver_tasks[key] = asyncio.ensure_future(
                        self.solver.solve_turnstile(keys[0], url, ts.get("action"), ts.get("cdata")))
            elif url not in job.ts_logged:
                job.ts_logged.add(url)
                job.log("Waiting for Turnstile widget to auto-pass…")

        # --- final page: resubmit if the API rejected a too-early request --------
        if job.reached_final and job.go_error and now - job.go_error_at > 3 and job.go_resubmits < 4:
            job.go_resubmits += 1
            job.go_error = None
            await self._eval(page, SUBMIT_GO_FORM_JS)
            return page

        # --- click the best "next step" button -------------------------------------
        for cand in snap.get("candidates") or []:
            if ts_pending and cand["score"] >= 90 and job.reached_final:
                break  # wait for token before pressing Get Link
            k = (url, cand["text"].lower())
            cnt, last = job.clicks.get(k, (0, 0.0))
            if now - last < 4 or cnt >= 8:
                continue
            cand_href = (cand.get("href") or "").lower()
            if job.base == "arolinks.com" and "bcsakhi." in host and (
                "readmore" in cand_href or "learn_more" in cand_href or "continue" in cand["text"].lower()
            ):
                elapsed = time.monotonic() - job.t0
                if elapsed < 102.0:
                    wait_left = 102.0 - elapsed
                    job.log(f"Waiting {wait_left:.0f}s on {host} for arolinks server-side timer (102s required)…")
                    while time.monotonic() - job.t0 < 102.0:
                        job.url_since = time.monotonic()
                        await asyncio.sleep(min(2.0, 102.0 - (time.monotonic() - job.t0)))
                    now = time.monotonic()
            job.clicks[k] = (cnt + 1, now)
            job.log(f"Click “{cand['text']}”")
            await self._click(job, page, cand["id"])
            await asyncio.sleep(0.6)
            return page

        # --- "click on an ad, wait, come back" gates -------------------------------
        if snap.get("adsHint") and not ts_pending and now - job.last_ad_end > 2.5:
            n = job.ad_attempts.get(url, 0)
            plan = [2.5, 11.0, 21.0, 21.0]
            if n < len(plan):
                job.ad_attempts[url] = n + 1
                dur = plan[n]
                job.log(f"Faking an ad visit ({dur:.0f}s away from page, no real ad click)")
                await self._eval(page, "() => window.__g2sAway && window.__g2sAway(true)")
                await asyncio.sleep(dur)
                await self._eval(page, "() => window.__g2sAway && window.__g2sAway(false)")
                job.last_ad_end = time.monotonic()
                return page

        if snap.get("waitHint") and url not in job.timer_logged:
            job.timer_logged.add(url)
            accel = (self._accel < 1 and not on_base
                     and not any(host == h or host.endswith("." + h) for h in cfg.no_accel_hosts))
            job.log(f"Page timer detected — accelerated x{1/self._accel:.0f}" if accel
                    else "Page timer detected — running in real time (server-checked)")

        # --- scroll (some steps only reveal the button near the bottom) ----------
        if not snap.get("atBottom"):
            try:
                await self._eval(page, "() => window.scrollBy(0, Math.round(window.innerHeight * 0.9))")
            except Exception:
                pass
        return page

    async def _click(self, job, page, gid):
        try:
            direct = await self._eval(page, r"""(gid) => {
              const el = document.querySelector('[data-g2s-id="' + gid + '"]');
              if (!el) return null;
              if (el.id === 'startCountdownBtn') {
                el.click();
                return 'verify-btn';
              }
              const a = el.closest('a[href]');
              if (!a || !a.href || a.href.startsWith('javascript:')) return null;
              const oc = a.getAttribute('onclick') || '';
              if (/specialActionHandler/i.test(oc)) {
                let target = null;
                const origOpen = window.open;
                window.open = function(u) { target = u; return window; };
                try {
                  if (typeof window.specialActionHandler === 'function') {
                    window.specialActionHandler({ preventDefault() {} });
                  } else {
                    el.click();
                  }
                } catch (e) {}
                window.open = origOpen;
                const navUrl = target || window.location.href;
                setTimeout(() => { window.location.href = navUrl; }, 50);
                return 'nav:' + navUrl;
              }
              if (a.id === 'cross-snp2' || /\/readmore/i.test(a.href)) {
                document.cookie = '_uocat=value; path=/; max-age=86400';
                try { el.click(); } catch (e) {}
                window.location.href = a.href;
                return 'nav:' + a.href;
              }
              if (/learn_more\\.php/i.test(a.href)) {
                window.location.href = a.href;
                return 'nav:' + a.href;
              }
              return null;
            }""", gid)
            if direct:
                if str(direct).startswith("nav:"):
                    await asyncio.sleep(2.0)
                return
        except Exception:
            pass
        loc = page.locator(f'[data-g2s-id="{gid}"]').first
        try:
            await loc.scroll_into_view_if_needed(timeout=1500)
            await loc.click(timeout=2500)
            return
        except Exception:
            pass
        try:
            n = await self._eval(page, CLOSE_OVERLAYS_JS)
            if n:
                job.log("Closed a floating overlay")
                await loc.click(timeout=1500)
                return
        except Exception:
            pass
        try:
            await self._eval(page, JS_CLICK, gid)
        except Exception:
            pass

    async def _click_cf_checkbox(self, page):
        try:
            for fr in page.frames:
                if CF_HOST in (fr.url or ""):
                    el = await fr.frame_element()
                    box = await el.bounding_box()
                    if box and box["width"] > 50:
                        await page.mouse.click(box["x"] + 28, box["y"] + box["height"] / 2)
                        return True
        except Exception:
            pass
        return False
