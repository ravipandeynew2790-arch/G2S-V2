"""
MongoDB + local JSON persistence & real-time synchronization layer for TheDrunkBots G2S.
Handles:
  - Live settings & credentials (proxy/tunnel, bot token, api_id, api_hash, owner/admin IDs,
    web admin password, force-subscribe channels, welcome message, start photo, etc.)
  - Cloud link cache (full CRUD: get, add/upsert, edit, delete, clear, search)
  - Complete error logs (stored for Web Admin Panel + forwarded to Admin on Telegram)
  - Telegram users registry (stats, ban/unban, broadcast targets)
"""
import asyncio
import json
import os
import time
import uuid
from typing import Any

try:
    from motor.motor_asyncio import AsyncIOMotorClient
    HAS_MOTOR = True
except ImportError:  # pragma: no cover
    AsyncIOMotorClient = None
    HAS_MOTOR = False

DEFAULT_MONGO_URI = (
    "mongodb+srv://shauryasharma9940_db_user:hi1OxTU3GdXFtRLf"
    "@g2sv2.fnvvosz.mongodb.net/?appName=G2SV2"
)
DEFAULT_START_PHOTO = "https://i.ibb.co/1Nx1jMn/IMG-20261005-021725.png"
DEFAULT_BRAND = "TheDrunkBots"
DEFAULT_WELCOME = (
    "👋 <b>Hey {first_name}!</b>\n"
    "<blockquote>⚡ <b>Send me any supported shortlink and I will bypass ads, "
    "timers & captchas automatically to give you the direct destination link!</b></blockquote>\n\n"
    "✨ <b>𝗙𝗘𝗔𝗧𝗨𝗥𝗘𝗦 :</b>\n"
    "<blockquote>• 🚀 <b>Instant Cloud Cache</b> (0.01s for repeated links)\n"
    "• 📊 <b>Live Progress Bar & ETA</b> while bypassing\n"
    "• 🛡 <b>Auto Turnstile & Multi-Step Solver</b>\n"
    "• 🔗 <b>15+ Shorteners Supported</b></blockquote>\n"
    "━━━━━━━━━━━━━━━━━━━━━━\n"
    "🏷 <b>𝗣𝗢𝗪𝗘𝗥𝗘𝗗 𝗕𝗬 :</b> <code>{brand}</code>"
)


def norm_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url.rstrip("/")


def _clean_int_list(val: Any) -> list[int]:
    if not val:
        return []
    if isinstance(val, (int, float)):
        return [int(val)]
    if isinstance(val, str):
        parts = [p.strip() for p in val.replace(";", ",").split(",") if p.strip()]
    elif isinstance(val, (list, tuple, set)):
        parts = list(val)
    else:
        return []
    out = []
    for p in parts:
        try:
            i = int(str(p).strip())
            if i and i not in out:
                out.append(i)
        except Exception:
            pass
    return out


class Database:
    def __init__(self, cfg_path: str, cache_path: str):
        self.cfg_path = cfg_path
        self.cache_path = cache_path
        self.client: Any = None
        self.db: Any = None
        self.connected: bool = False
        self.last_error: str | None = None
        self._lock = asyncio.Lock()

        # Local in-memory mirrors (always available even if Mongo is momentarily unreachable)
        self.settings: dict[str, Any] = self._load_initial_settings()
        self.local_cache: dict[str, dict] = self._load_local_cache()
        self.local_logs: list[dict] = []
        self.local_users: dict[int, dict] = {}
        self._on_settings_change_cbs = []

    def on_settings_change(self, cb):
        self._on_settings_change_cbs.append(cb)

    def _load_initial_settings(self) -> dict[str, Any]:
        raw = {}
        if os.path.exists(self.cfg_path):
            try:
                with open(self.cfg_path, encoding="utf-8") as f:
                    raw = json.load(f)
            except Exception:
                raw = {}

        owner_raw = os.environ.get("G2S_OWNER_ID") or os.environ.get("OWNER_ID") or raw.get("owner_id") or 0
        try:
            owner_id = int(str(owner_raw).strip()) if str(owner_raw).strip() else 0
        except Exception:
            owner_id = 0

        admin_ids = _clean_int_list(
            os.environ.get("G2S_ADMIN_IDS") or os.environ.get("ADMIN_IDS") or raw.get("admin_ids") or []
        )
        if owner_id and owner_id not in admin_ids:
            admin_ids.insert(0, owner_id)

        api_id_raw = (
            os.environ.get("G2S_API_ID")
            or os.environ.get("TELEGRAM_API_ID")
            or os.environ.get("API_ID")
            or raw.get("telegram_api_id")
            or 0
        )
        try:
            api_id = int(str(api_id_raw).strip()) if str(api_id_raw).strip() else 0
        except Exception:
            api_id = 0

        return {
            "mongo_uri": (
                os.environ.get("G2S_MONGO_URI")
                or os.environ.get("MONGO_URI")
                or raw.get("mongo_uri")
                or DEFAULT_MONGO_URI
            ),
            "brand_name": raw.get("brand_name") or DEFAULT_BRAND,
            "admin_password": (
                os.environ.get("G2S_ADMIN_PASSWORD")
                or os.environ.get("ADMIN_PASSWORD")
                or raw.get("admin_password")
                or "admin123"
            ),
            "telegram_bot_token": (
                os.environ.get("G2S_BOT_TOKEN")
                or os.environ.get("TELEGRAM_BOT_TOKEN")
                or os.environ.get("BOT_TOKEN")
                or raw.get("telegram_bot_token")
                or ""
            ).strip(),
            "telegram_api_id": api_id,
            "telegram_api_hash": (
                os.environ.get("G2S_API_HASH")
                or os.environ.get("TELEGRAM_API_HASH")
                or os.environ.get("API_HASH")
                or raw.get("telegram_api_hash")
                or ""
            ).strip(),
            "owner_id": owner_id,
            "admin_ids": admin_ids,
            "start_photo": (
                os.environ.get("G2S_START_PHOTO")
                or raw.get("start_photo")
                or DEFAULT_START_PHOTO
            ).strip(),
            "welcome_message": raw.get("welcome_message") or DEFAULT_WELCOME,
            "updates_channel": raw.get("updates_channel") or "https://t.me/TheDrunkBots",
            "force_sub_enabled": bool(raw.get("force_sub_enabled", True)),
            "force_sub_channels": list(raw.get("force_sub_channels") or []),
            "maintenance_mode": bool(raw.get("maintenance_mode", False)),
            "protect_content": bool(raw.get("protect_content", False)),
            "proxy": (
                os.environ.get("G2S_PROXY")
                if "G2S_PROXY" in os.environ
                else raw.get("proxy")
            ),
            "proxy_expired": bool(raw.get("proxy_expired", False)),
            "proxy_expired_notified_for": str(raw.get("proxy_expired_notified_for") or ""),
            "solver_url": (
                os.environ.get("G2S_SOLVER_URL")
                or raw.get("solver_url")
                or "https://cf.vltcx.eu.cc"
            ).rstrip("/"),
            "solver_api_key": os.environ.get("G2S_SOLVER_API_KEY") or raw.get("solver_api_key"),
            "accel_factor": float(os.environ.get("G2S_ACCEL_FACTOR") or raw.get("accel_factor", 0.1)),
            "timeout": int(os.environ.get("G2S_TIMEOUT") or raw.get("timeout", 300)),
            "stuck_seconds": int(raw.get("stuck_seconds", 100)),
            "cache_enabled": bool(raw.get("cache_enabled", True)),
            "cache_hours": float(os.environ.get("G2S_CACHE_HOURS") or raw.get("cache_hours", 24)),
            "headless": bool(raw.get("headless", False)),
            "show_browser": bool(raw.get("show_browser", False)),
            "no_accel_hosts": list(raw.get("no_accel_hosts") or [
                "get2short.com", "liteshort.com", "nowshort.com", "linksflys.com",
                "vplink.in", "arolinks.com", "babylinks.in", "alpha-links.in", "urllinkshort.in",
                "lksfy.com", "softurl.in", "shortwala.com", "earnlinks.in", "inshorturl.in", "inshorturl.com"
            ]),
            "stats": {
                "total_bypasses": 0,
                "cache_hits": 0,
                "failed_bypasses": 0,
            },
        }

    def _load_local_cache(self) -> dict[str, dict]:
        if os.path.exists(self.cache_path):
            try:
                with open(self.cache_path, encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        return data
            except Exception:
                pass
        return {}

    def _save_local_cache(self):
        try:
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(self.local_cache, f, indent=1)
        except Exception:
            pass

    def save_config_file(self):
        try:
            raw = {}
            if os.path.exists(self.cfg_path):
                try:
                    with open(self.cfg_path, encoding="utf-8") as f:
                        raw = json.load(f)
                except Exception:
                    raw = {}
            keys = (
                "mongo_uri", "brand_name", "admin_password",
                "telegram_bot_token", "telegram_api_id", "telegram_api_hash",
                "owner_id", "admin_ids", "start_photo", "welcome_message",
                "updates_channel", "force_sub_enabled", "force_sub_channels",
                "maintenance_mode", "protect_content", "proxy", "proxy_expired",
                "proxy_expired_notified_for",
                "solver_url", "solver_api_key", "accel_factor", "timeout",
                "stuck_seconds", "cache_enabled", "cache_hours", "no_accel_hosts",
            )
            for k in keys:
                if k in self.settings:
                    raw[k] = self.settings[k]
            with open(self.cfg_path, "w", encoding="utf-8") as f:
                json.dump(raw, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print("save_config_file error:", e, flush=True)

    async def connect(self):
        uri = (self.settings.get("mongo_uri") or DEFAULT_MONGO_URI).strip()
        if not HAS_MOTOR or not uri:
            self.connected = False
            self.last_error = "motor not installed or mongo_uri empty"
            return False
        try:
            self.client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=10000)
            await self.client.server_info()
            self.db = self.client["g2sv2"]
            self.connected = True
            self.last_error = None
            await self._sync_on_startup()
            print("✔ Connected to MongoDB Atlas (g2sv2)", flush=True)
            return True
        except Exception as e:
            self.connected = False
            self.last_error = f"{type(e).__name__}: {e}"
            print(f"⚠ MongoDB connection failed ({self.last_error}) — using local JSON fallback", flush=True)
            return False

    async def _sync_on_startup(self):
        if not self.connected or self.db is None:
            return
        try:
            doc = await self.db.settings.find_one({"_id": "global"})
            if doc:
                doc.pop("_id", None)
                env_token = bool(
                    os.environ.get("G2S_BOT_TOKEN")
                    or os.environ.get("TELEGRAM_BOT_TOKEN")
                    or os.environ.get("BOT_TOKEN")
                )
                env_proxy = "G2S_PROXY" in os.environ
                for k, v in doc.items():
                    if k == "telegram_bot_token":
                        if env_token or not str(v or "").strip():
                            continue
                    elif k == "proxy":
                        if env_proxy or not str(v or "").strip():
                            continue
                    elif k in ("telegram_api_hash", "admin_password"):
                        if not str(v or "").strip():
                            continue
                    elif k in ("telegram_api_id", "owner_id"):
                        if not v:
                            continue
                    elif k == "admin_ids" and self.settings.get("admin_ids"):
                        merged = list(self.settings["admin_ids"])
                        for aid in _clean_int_list(v):
                            if aid not in merged:
                                merged.append(aid)
                        self.settings["admin_ids"] = merged
                        continue
                    if v is not None:
                        self.settings[k] = v
                if self.settings.get("owner_id") and self.settings["owner_id"] not in self.settings["admin_ids"]:
                    self.settings["admin_ids"].insert(0, int(self.settings["owner_id"]))
            await self.db.settings.update_one(
                {"_id": "global"},
                {"$set": {**self.settings, "updated_at": time.time()}},
                upsert=True,
            )
            self.save_config_file()

            # Purge any poisoned arolinks/vplink local cache entries (<103s elapsed)
            for src, item in list(self.local_cache.items()):
                if ("arolinks.com/" in src or "vplink.in/" in src) and float(item.get("elapsed") or 0) < 103.0 and item.get("method") != "manual":
                    self.local_cache.pop(src, None)

            # Load Mongo cache into local mirror & push any valid local cache items to Mongo
            async for item in self.db.cache.find({}):
                src = item.get("source") or item.get("_id")
                if src:
                    if ("arolinks.com/" in src or "vplink.in/" in src) and float(item.get("elapsed") or 0) < 103.0 and item.get("method") != "manual":
                        await self.db.cache.delete_one({"_id": item.get("_id")})
                        continue
                    clean = {k: v for k, v in item.items() if k != "_id"}
                    self.local_cache[src] = clean
            for src, item in list(self.local_cache.items()):
                await self.db.cache.update_one(
                    {"_id": src},
                    {"$set": {**item, "source": src}},
                    upsert=True,
                )
            self._save_local_cache()
        except Exception as e:
            print("MongoDB startup sync warning:", e, flush=True)

    async def refresh_settings_from_mongo(self) -> dict[str, Any]:
        if self.connected and self.db is not None:
            try:
                doc = await self.db.settings.find_one({"_id": "global"})
                if doc:
                    doc.pop("_id", None)
                    self.settings.update(doc)
            except Exception:
                pass
        return dict(self.settings)

    async def update_settings(self, patch: dict[str, Any], notify: bool = True) -> dict[str, Any]:
        async with self._lock:
            changed_keys = set()
            for k, v in patch.items():
                if k == "owner_id":
                    try:
                        v = int(str(v).strip()) if str(v).strip() else 0
                    except Exception:
                        v = self.settings.get("owner_id", 0)
                elif k == "telegram_api_id":
                    try:
                        v = int(str(v).strip()) if str(v).strip() else 0
                    except Exception:
                        v = self.settings.get("telegram_api_id", 0)
                elif k == "admin_ids":
                    v = _clean_int_list(v)
                elif k == "accel_factor":
                    try:
                        v = max(0.05, min(1.0, float(v)))
                    except Exception:
                        v = self.settings.get("accel_factor", 0.1)
                elif k == "timeout":
                    try:
                        v = max(30, min(900, int(v)))
                    except Exception:
                        v = self.settings.get("timeout", 300)
                elif k == "cache_hours":
                    try:
                        v = max(0.1, float(v))
                    except Exception:
                        v = self.settings.get("cache_hours", 24.0)
                elif k == "proxy":
                    v = (v or "").strip() or None
                if self.settings.get(k) != v:
                    changed_keys.add(k)
                self.settings[k] = v

            owner_id = self.settings.get("owner_id") or 0
            admin_ids = _clean_int_list(self.settings.get("admin_ids") or [])
            if owner_id and owner_id not in admin_ids:
                admin_ids.insert(0, owner_id)
            self.settings["admin_ids"] = admin_ids

            self.save_config_file()
            if self.connected and self.db is not None:
                try:
                    await self.db.settings.update_one(
                        {"_id": "global"},
                        {"$set": {**self.settings, "updated_at": time.time()}},
                        upsert=True,
                    )
                except Exception as e:
                    print("MongoDB update_settings warning:", e, flush=True)

        if notify and changed_keys:
            for cb in self._on_settings_change_cbs:
                try:
                    res = cb(changed_keys, dict(self.settings))
                    if asyncio.iscoroutine(res):
                        await res
                except Exception as e:
                    print("settings callback error:", e, flush=True)
        return dict(self.settings)

    def is_admin(self, user_id: int | str | None) -> bool:
        if not user_id:
            return False
        try:
            uid = int(user_id)
        except Exception:
            return False
        owner = int(self.settings.get("owner_id") or 0)
        admins = _clean_int_list(self.settings.get("admin_ids") or [])
        return (uid == owner and owner != 0) or (uid in admins)

    def is_owner(self, user_id: int | str | None) -> bool:
        if not user_id:
            return False
        try:
            uid = int(user_id)
        except Exception:
            return False
        owner = int(self.settings.get("owner_id") or 0)
        return (uid == owner and owner != 0) or self.is_admin(uid)

    # ------------------------------------------------------------------- stats
    async def increment_stat(self, key: str, delta: int = 1):
        stats = self.settings.setdefault("stats", {"total_bypasses": 0, "cache_hits": 0, "failed_bypasses": 0})
        stats[key] = int(stats.get(key, 0)) + delta
        if self.connected and self.db is not None:
            try:
                await self.db.settings.update_one(
                    {"_id": "global"},
                    {"$inc": {f"stats.{key}": delta}},
                    upsert=True,
                )
            except Exception:
                pass

    # ------------------------------------------------------------------- cache
    async def get_cache(self, url: str, force: bool = False) -> dict | None:
        if force or not self.settings.get("cache_enabled", True):
            return None
        key = norm_url(url)
        if not key:
            return None
        ttl = float(self.settings.get("cache_hours", 24)) * 3600.0
        now = time.time()

        keys_to_try = [key]
        if "://vplink.in/" in key:
            prefix, _, slug = key.rpartition("/")
            if slug.startswith("__") and slug.endswith("__") and len(slug) > 4:
                keys_to_try.append(f"{prefix}/{slug[2:-2]}")
            elif slug:
                keys_to_try.append(f"{prefix}/__{slug}__")

        hit = None
        hit_key = key
        for k_try in keys_to_try:
            cand = self.local_cache.get(k_try)
            if (not cand or now - float(cand.get("at", 0)) >= ttl) and self.connected and self.db is not None:
                try:
                    doc = await self.db.cache.find_one({"_id": k_try})
                    if doc:
                        doc.pop("_id", None)
                        self.local_cache[k_try] = doc
                        cand = doc
                except Exception:
                    pass
            if cand:
                if ("arolinks.com/" in k_try or "vplink.in/" in k_try) and float(cand.get("elapsed") or 0) < 103.0 and cand.get("method") != "manual":
                    await self.delete_cache(k_try)
                    continue
                if now - float(cand.get("at", 0)) < ttl:
                    hit = cand
                    hit_key = k_try
                    break

        if not hit:
            return None

        hit["hits"] = int(hit.get("hits", 0)) + 1
        self.local_cache[hit_key] = hit

        async def _bg_inc():
            if self.connected and self.db is not None:
                try:
                    await self.db.cache.update_one({"_id": hit_key}, {"$inc": {"hits": 1}})
                except Exception:
                    pass
            await self.increment_stat("cache_hits", 1)

        asyncio.create_task(_bg_inc())
        return {**hit, "source": key, "cached": True}

    async def set_cache(self, source_url: str, data: dict) -> dict:
        key = norm_url(source_url)
        entry = {
            "source": key,
            "url": (data.get("url") or "").strip(),
            "elapsed": float(data.get("elapsed") or 0.0),
            "method": data.get("method") or "manual",
            "steps": list(data.get("steps") or []),
            "at": float(data.get("at") or time.time()),
            "hits": int(data.get("hits") or 0),
        }
        self.local_cache[key] = entry
        self._save_local_cache()
        if self.connected and self.db is not None:
            try:
                await self.db.cache.update_one(
                    {"_id": key},
                    {"$set": entry},
                    upsert=True,
                )
            except Exception as e:
                print("MongoDB set_cache warning:", e, flush=True)
        return entry

    async def delete_cache(self, source_url: str) -> bool:
        key = norm_url(source_url)
        existed = key in self.local_cache
        self.local_cache.pop(key, None)
        if source_url in self.local_cache:
            self.local_cache.pop(source_url, None)
            existed = True
        self._save_local_cache()
        if self.connected and self.db is not None:
            try:
                r = await self.db.cache.delete_one({"_id": key})
                if r.deleted_count:
                    existed = True
            except Exception:
                pass
        return existed

    async def clear_cache(self) -> int:
        cnt = len(self.local_cache)
        self.local_cache.clear()
        self._save_local_cache()
        if self.connected and self.db is not None:
            try:
                r = await self.db.cache.delete_many({})
                cnt = max(cnt, r.deleted_count)
            except Exception:
                pass
        return cnt

    async def list_cache(self, search: str = "", limit: int = 200) -> list[dict]:
        items = []
        if self.connected and self.db is not None:
            try:
                cursor = self.db.cache.find({}).sort("at", -1).limit(500)
                async for doc in cursor:
                    src = doc.get("source") or doc.get("_id")
                    clean = {k: v for k, v in doc.items() if k != "_id"}
                    clean["source"] = src
                    self.local_cache[src] = clean
                    items.append(clean)
            except Exception:
                items = [{"source": k, **v} for k, v in self.local_cache.items()]
        else:
            items = [{"source": k, **v} for k, v in self.local_cache.items()]

        items.sort(key=lambda x: -float(x.get("at", 0)))
        if search:
            q = search.lower().strip()
            items = [
                i for i in items
                if q in (i.get("source") or "").lower() or q in (i.get("url") or "").lower()
            ]
        return items[:limit]

    # -------------------------------------------------------------- error logs
    async def add_error_log(
        self,
        source: str,
        error: str,
        log_lines: list[str],
        origin: str = "web",
        user_id: int | None = None,
        username: str | None = None,
    ) -> dict:
        lid = uuid.uuid4().hex[:10]
        entry = {
            "id": lid,
            "source": norm_url(source) or source,
            "error": str(error),
            "log": list(log_lines or []),
            "origin": origin,
            "user_id": user_id,
            "username": username,
            "at": time.time(),
        }
        self.local_logs.insert(0, entry)
        self.local_logs = self.local_logs[:200]
        if self.connected and self.db is not None:
            try:
                await self.db.error_logs.insert_one({"_id": lid, **entry})
            except Exception:
                pass
        await self.increment_stat("failed_bypasses", 1)
        return entry

    async def list_error_logs(self, limit: int = 100) -> list[dict]:
        if self.connected and self.db is not None:
            try:
                out = []
                cursor = self.db.error_logs.find({}).sort("at", -1).limit(limit)
                async for doc in cursor:
                    doc.pop("_id", None)
                    out.append(doc)
                self.local_logs = out
                return out
            except Exception:
                pass
        return self.local_logs[:limit]

    async def delete_error_log(self, log_id: str) -> bool:
        before = len(self.local_logs)
        self.local_logs = [x for x in self.local_logs if x.get("id") != log_id]
        deleted = len(self.local_logs) < before
        if self.connected and self.db is not None:
            try:
                r = await self.db.error_logs.delete_one({"_id": log_id})
                if r.deleted_count:
                    deleted = True
            except Exception:
                pass
        return deleted

    async def clear_error_logs(self) -> int:
        cnt = len(self.local_logs)
        self.local_logs.clear()
        if self.connected and self.db is not None:
            try:
                r = await self.db.error_logs.delete_many({})
                cnt = max(cnt, r.deleted_count)
            except Exception:
                pass
        return cnt

    # ------------------------------------------------------------------- users
    async def touch_user(self, tg_user: dict, increment_bypass: bool = False) -> dict:
        uid = int(tg_user.get("id") or tg_user.get("user_id") or 0)
        if not uid:
            return {}
        now = time.time()
        existing = self.local_users.get(uid)
        if not existing and self.connected and self.db is not None:
            try:
                doc = await self.db.users.find_one({"_id": uid})
                if doc:
                    doc.pop("_id", None)
                    existing = doc
            except Exception:
                pass

        user_doc = {
            "user_id": uid,
            "username": tg_user.get("username") or (existing or {}).get("username") or "",
            "first_name": tg_user.get("first_name") or (existing or {}).get("first_name") or "User",
            "last_name": tg_user.get("last_name") or (existing or {}).get("last_name") or "",
            "joined_at": float((existing or {}).get("joined_at") or now),
            "last_active": now,
            "bypass_count": int((existing or {}).get("bypass_count") or 0) + (1 if increment_bypass else 0),
            "banned": bool((existing or {}).get("banned", False)),
        }
        self.local_users[uid] = user_doc
        if self.connected and self.db is not None:
            try:
                await self.db.users.update_one(
                    {"_id": uid},
                    {"$set": user_doc},
                    upsert=True,
                )
            except Exception:
                pass
        return user_doc

    async def get_user(self, user_id: int) -> dict | None:
        uid = int(user_id)
        if self.connected and self.db is not None:
            try:
                doc = await self.db.users.find_one({"_id": uid})
                if doc:
                    doc.pop("_id", None)
                    self.local_users[uid] = doc
                    return doc
            except Exception:
                pass
        return self.local_users.get(uid)

    async def set_user_banned(self, user_id: int, banned: bool) -> bool:
        uid = int(user_id)
        doc = await self.get_user(uid) or {
            "user_id": uid,
            "username": "",
            "first_name": "User",
            "last_name": "",
            "joined_at": time.time(),
            "last_active": time.time(),
            "bypass_count": 0,
        }
        doc["banned"] = bool(banned)
        self.local_users[uid] = doc
        if self.connected and self.db is not None:
            try:
                await self.db.users.update_one(
                    {"_id": uid},
                    {"$set": doc},
                    upsert=True,
                )
            except Exception:
                pass
        return True

    async def delete_user(self, user_id: int) -> bool:
        uid = int(user_id)
        existed = uid in self.local_users
        self.local_users.pop(uid, None)
        if self.connected and self.db is not None:
            try:
                r = await self.db.users.delete_one({"_id": uid})
                if r.deleted_count:
                    existed = True
            except Exception:
                pass
        return existed

    async def list_users(self, search: str = "", limit: int = 200) -> list[dict]:
        users = []
        if self.connected and self.db is not None:
            try:
                cursor = self.db.users.find({}).sort("last_active", -1).limit(500)
                async for doc in cursor:
                    doc.pop("_id", None)
                    uid = int(doc.get("user_id") or 0)
                    if uid:
                        self.local_users[uid] = doc
                        users.append(doc)
            except Exception:
                users = list(self.local_users.values())
        else:
            users = list(self.local_users.values())
        users.sort(key=lambda x: -float(x.get("last_active", 0)))
        if search:
            q = search.lower().strip()
            users = [
                u for u in users
                if q in str(u.get("user_id", "")).lower()
                or q in (u.get("username") or "").lower()
                or q in (u.get("first_name") or "").lower()
            ]
        return users[:limit]

    async def get_all_user_ids(self, include_banned: bool = False) -> list[int]:
        users = await self.list_users(limit=10000)
        return [
            int(u["user_id"])
            for u in users
            if u.get("user_id") and (include_banned or not u.get("banned"))
        ]
