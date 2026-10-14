"""
TheDrunkBots Telegram Bot Engine for G2S Direct-Link Generator.
Features:
  - Matches attached screenshot formatting (blockquotes, 14-segment live progress bar,
    estimated time remaining, elapsed time, COPY LINK + OPEN LINK inline buttons)
  - /start welcome message with photo https://i.ibb.co/1Nx1jMn/IMG-20261005-021725.png
  - Force-Subscribe check & interactive channel management
  - Full interactive Admin Panel inside Telegram (only visible to owner_id / admin_ids)
  - Instant MongoDB cache lookup & storage (0.01s response on cached links)
  - Zero technical logs shown to users; complete error logs stored in MongoDB/Web UI
    and forwarded to Owner/Admins on Telegram
  - Automatic Owner alert on Telegram when the Pinggy tunnel / proxy expires
"""
import asyncio
import html
import re
import time
from typing import Any
from urllib.parse import urlparse

import aiohttp

from .db import DEFAULT_BRAND, DEFAULT_START_PHOTO, Database, norm_url

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)


def esc(val: Any) -> str:
    return html.escape(str(val or ""))


def estimate_duration(url: str) -> float:
    h = (urlparse(url).hostname or "").lower()
    if "arolinks." in h or "vplink." in h:
        return 115.0
    if "inshorturl." in h:
        return 90.0
    if "urlking." in h or "mytoolspro." in h:
        return 4.0
    if "linksflys." in h:
        return 12.0
    if "get2short." in h or "liteshort." in h or "nowshort." in h:
        return 48.0
    if "softurl." in h or "jobsraavi." in h or "alpha-links." in h:
        return 35.0
    if "vipshort." in h:
        return 25.0
    if "babylinks." in h:
        return 18.0
    if "linksfire." in h or "urlspay." in h or "v2links." in h or "makelinks." in h:
        return 10.0
    if "urllinkshort." in h or "shortwala." in h or "earnlinks." in h or "lksfy." in h:
        return 20.0
    return 26.0


def build_progress_bar(pct: int, width: int = 14) -> str:
    pct = max(1, min(99, int(pct)))
    filled = max(1, min(width, round((pct / 100.0) * width)))
    return "▰" * filled + "▱" * (width - filled)


def format_fetching_message(pct: int, elapsed: float, eta: float) -> str:
    bar = build_progress_bar(pct, 14)
    eta_str = f"~{max(1, int(round(eta)))}s"
    return (
        f"<blockquote>⚡ <b>𝗙𝗘𝗧𝗖𝗛𝗜𝗡𝗚 𝗟𝗜𝗡𝗞...</b>\n"
        f"<code>{bar}</code>  <b>{pct}%</b>\n"
        f"⏳ <b>𝗘𝗧𝗔 :</b> <code>{eta_str}</code>  •  ⏱️ <b>𝗘𝗟𝗔𝗣𝗦𝗘𝗗 :</b> <code>{elapsed:.1f}s</code></blockquote>"
    )


def format_success_message(source_url: str, bypassed_url: str, elapsed: float, cached: bool = False) -> str:
    status_label = "CACHED ⚡" if cached else "SUCCESS"
    t_str = "0.01s" if cached and elapsed < 0.05 else f"{elapsed:.2f}s"
    return (
        f"🟢 • <b>𝗦𝗨𝗖𝗖𝗘𝗦𝗦</b>\n\n"
        f"🔗 <b>𝗢𝗥𝗜𝗚𝗜𝗡𝗔𝗟 𝗟𝗜𝗡𝗞 :</b>\n"
        f"<blockquote>{esc(source_url)}</blockquote>\n"
        f"✅ <b>𝗕𝗬𝗣𝗔𝗦𝗦𝗘𝗗 𝗟𝗜𝗡𝗞 :</b>\n"
        f"<blockquote>{esc(bypassed_url)}</blockquote>\n"
        f"<blockquote>⏱️ <b>𝗧𝗜𝗠𝗘 𝗧𝗔𝗞𝗘𝗡 :</b> <code>{t_str}</code>  •  🟢 <b>𝗦𝗧𝗔𝗧𝗨𝗦 :</b> <code>{status_label}</code></blockquote>"
    )


def format_user_error_message(source_url: str) -> str:
    return (
        f"🔴 • <b>𝗙𝗔𝗜𝗟𝗘𝗗</b>\n\n"
        f"🔗 <b>𝗢𝗥𝗜𝗚𝗜𝗡𝗔𝗟 𝗟𝗜𝗡𝗞 :</b>\n"
        f"<blockquote>{esc(source_url)}</blockquote>\n"
        f"<blockquote>⚠️ <b>𝗦𝗧𝗔𝗧𝗨𝗦 :</b> <code>Unable to bypass this link right now. Admin has been notified.</code></blockquote>"
    )


class TelegramBot:
    def __init__(self, db: Database, resolve_fn, proxy_test_fn, set_proxy_fn):
        self.db = db
        self.resolve_fn = resolve_fn
        self.proxy_test_fn = proxy_test_fn
        self.set_proxy_fn = set_proxy_fn

        self.token: str = ""
        self._invalid_token: str = ""
        self.bot_info: dict | None = None
        self.running: bool = False
        self._task: asyncio.Task | None = None
        self._session: aiohttp.ClientSession | None = None
        self._offset: int = 0
        self._photo_file_id_cache: dict[str, str] = {}
        # Tracks interactive admin input state: {user_id: {"action": "...", ...}}
        self._admin_states: dict[int, dict[str, Any]] = {}
        self._last_tunnel_alert_at: float = 0.0
        self._alerted_expired_proxy: str | None = None
        # Tracks active/queued bypass jobs for accurate queue-aware ETA: {job_uid: [start_mono, est_sec]}
        self._active_jobs: dict[str, list[float]] = {}

    @property
    def api_base(self) -> str:
        return f"https://api.telegram.org/bot{self.token}"

    async def start(self):
        token = (self.db.settings.get("telegram_bot_token") or "").strip()
        if not token:
            print("Telegram Bot: no telegram_bot_token configured yet (can be set in config.json or Admin Panel).", flush=True)
            return False
        if self.running and token == self.token and self._task and not self._task.done():
            return True
        await self.stop()
        self.token = token
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45))
        me = await self.api("getMe")
        if not me or not me.get("ok"):
            err = (me or {}).get("description", "invalid token or network error")
            if "unauthorized" in str(err).lower():
                self._invalid_token = token
            print(f"⚠ Telegram Bot failed to start: {err}", flush=True)
            await self.stop()
            return False
        self._invalid_token = ""
        self.bot_info = me.get("result") or {}
        uname = self.bot_info.get("username", "unknown")
        print(f"✔ Telegram Bot started: @{uname} (ID: {self.bot_info.get('id')})", flush=True)
        await self._register_commands()
        self.running = True
        self._task = asyncio.create_task(self._poll_loop())
        return True

    async def stop(self):
        self.running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except Exception:
                pass
        self._task = None
        if self._session:
            try:
                await self._session.close()
            except Exception:
                pass
        self._session = None
        self.bot_info = None

    async def reload_if_needed(self):
        new_token = (self.db.settings.get("telegram_bot_token") or "").strip()
        if new_token and new_token == self._invalid_token:
            return
        if new_token != self.token or (new_token and not self.running):
            if new_token:
                await self.start()
            else:
                await self.stop()

    async def api(self, method: str, payload: dict | None = None, timeout: float = 35.0) -> dict | None:
        if not self.token or not self._session:
            return None
        try:
            async with self._session.post(
                f"{self.api_base}/{method}",
                json=payload or {},
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as r:
                return await r.json(content_type=None)
        except Exception as e:
            return {"ok": False, "description": str(e)}

    async def _register_commands(self):
        user_cmds = [
            {"command": "start", "description": "🍹 Start TheDrunkBots Bypasser"},
            {"command": "help", "description": "📖 How to bypass links & supported sites"},
            {"command": "bypass", "description": "⚡ Bypass a shortlink"},
            {"command": "myinfo", "description": "👤 View your stats & profile"},
            {"command": "status", "description": "📊 Check bot & cloud cache status"},
        ]
        await self.api("setMyCommands", {"commands": user_cmds})

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        reply_to: int | None = None,
        reply_markup: dict | None = None,
        disable_preview: bool = True,
    ) -> dict | None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": disable_preview,
        }
        if self.db.settings.get("protect_content"):
            payload["protect_content"] = True
        if reply_to:
            payload["reply_to_message_id"] = reply_to
        if reply_markup:
            payload["reply_markup"] = reply_markup
        res = await self.api("sendMessage", payload)
        # Fallback if older Bot API server rejects copy_text button
        if (not res or not res.get("ok")) and reply_markup and "copy_text" in str(reply_markup):
            fallback_rows = []
            for row in reply_markup.get("inline_keyboard", []):
                new_row = [b for b in row if "copy_text" not in b]
                if new_row:
                    fallback_rows.append(new_row)
            payload["reply_markup"] = {"inline_keyboard": fallback_rows}
            res = await self.api("sendMessage", payload)
        return res

    async def edit_message(
        self,
        chat_id: int | str,
        message_id: int,
        text: str,
        reply_markup: dict | None = None,
        disable_preview: bool = True,
    ) -> dict | None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": disable_preview,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        res = await self.api("editMessageText", payload)
        if (not res or not res.get("ok")) and reply_markup and "copy_text" in str(reply_markup):
            fallback_rows = []
            for row in reply_markup.get("inline_keyboard", []):
                new_row = [b for b in row if "copy_text" not in b]
                if new_row:
                    fallback_rows.append(new_row)
            payload["reply_markup"] = {"inline_keyboard": fallback_rows}
            res = await self.api("editMessageText", payload)
        return res

    async def send_photo(
        self,
        chat_id: int | str,
        photo_url: str,
        caption: str,
        reply_to: int | None = None,
        reply_markup: dict | None = None,
    ) -> dict | None:
        photo_ref = self._photo_file_id_cache.get(photo_url) or photo_url
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "photo": photo_ref,
            "caption": caption,
            "parse_mode": "HTML",
        }
        if reply_to:
            payload["reply_to_message_id"] = reply_to
        if reply_markup:
            payload["reply_markup"] = reply_markup
        res = await self.api("sendPhoto", payload)
        if res and res.get("ok"):
            photos = (res.get("result") or {}).get("photo") or []
            if photos and isinstance(photos, list):
                fid = photos[-1].get("file_id")
                if fid:
                    self._photo_file_id_cache[photo_url] = fid
            return res
        # Fallback to text message if image URL couldn't be fetched by Telegram
        return await self.send_message(chat_id, caption, reply_to=reply_to, reply_markup=reply_markup)

    # ------------------------------------------------- Owner / Admin alerts
    async def notify_admins(self, text: str, reply_markup: dict | None = None):
        if not self.running:
            return
        targets = set()
        owner = int(self.db.settings.get("owner_id") or 0)
        if owner:
            targets.add(owner)
        for aid in self.db.settings.get("admin_ids") or []:
            try:
                if int(aid):
                    targets.add(int(aid))
            except Exception:
                pass
        for tid in targets:
            try:
                await self.send_message(tid, text, reply_markup=reply_markup)
            except Exception:
                pass

    async def notify_error_log(self, entry: dict):
        if not self.running:
            return
        src = entry.get("source") or ""
        err = entry.get("error") or "Unknown error"
        uid = entry.get("user_id")
        uname = entry.get("username")
        origin = entry.get("origin", "bot")
        logs = entry.get("log") or []
        log_tail = "\n".join(logs[-18:]) if logs else "No step logs recorded."
        if len(log_tail) > 2600:
            log_tail = "…\n" + log_tail[-2600:]
        user_line = f"<code>{uid}</code> (@{esc(uname)})" if uid else f"<code>{esc(origin)}</code>"
        msg = (
            f"🚨 • <b>𝗕𝗬𝗣𝗔𝗦𝗦 𝗘𝗥𝗥𝗢𝗥 𝗥𝗘𝗣𝗢𝗥𝗧</b>\n\n"
            f"👤 <b>𝗨𝗦𝗘𝗥 / 𝗢𝗥𝗜𝗚𝗜𝗡 :</b> {user_line}\n"
            f"🔗 <b>𝗟𝗜𝗡𝗞 :</b>\n<blockquote>{esc(src)}</blockquote>\n"
            f"❌ <b>𝗘𝗥𝗥𝗢𝗥 :</b>\n<blockquote>{esc(err)}</blockquote>\n"
            f"📜 <b>𝗖𝗢𝗠𝗣𝗟𝗘𝗧𝗘 𝗦𝗧𝗘𝗣 𝗟𝗢𝗚 :</b>\n"
            f"<pre>{esc(log_tail)}</pre>"
        )
        kb = {
            "inline_keyboard": [
                [
                    {"text": "🛠 Open Admin Panel", "callback_data": "adm:home"},
                    {"text": "🌐 Set Tunnel Proxy", "callback_data": "adm:set_proxy"},
                ]
            ]
        }
        await self.notify_admins(msg, reply_markup=kb)

    async def notify_tunnel_expired(self, expired_proxy: str | None, reason: str):
        px_key = (expired_proxy or "").strip()
        if not px_key:
            return
        already_notified_for = (self.db.settings.get("proxy_expired_notified_for") or "").strip()
        if (
            self._alerted_expired_proxy == px_key
            or already_notified_for == px_key
            or bool(self.db.settings.get("proxy_expired"))
        ):
            return
        self._alerted_expired_proxy = px_key
        self._last_tunnel_alert_at = time.time()
        await self.db.update_settings(
            {"proxy_expired": True, "proxy_expired_notified_for": px_key},
            notify=False,
        )
        if not self.running:
            return
        masked = re.sub(r"(://[^:/@]+:)[^@]+@", r"\1•••@", px_key)
        msg = (
            f"⚠️ • <b>𝗧𝗨𝗡𝗡𝗘𝗟 / 𝗣𝗥𝗢𝗫𝗬 𝗘𝗫𝗣𝗜𝗥𝗘𝗗</b>\n\n"
            f"🌐 <b>𝗘𝗫𝗣𝗜𝗥𝗘𝗗 𝗧𝗨𝗡𝗡𝗘𝗟 :</b>\n<blockquote><code>{esc(masked)}</code></blockquote>\n"
            f"❗ <b>𝗥𝗘𝗔𝗦𝗢𝗡 :</b>\n<blockquote><code>{esc(reason[:300])}</code></blockquote>\n"
            f"<blockquote>🔄 <b>Please renew your Pinggy / residential tunnel and update it below or in the Web Admin Panel!</b></blockquote>"
        )
        kb = {
            "inline_keyboard": [
                [{"text": "🌐 Update Tunnel Proxy Now", "callback_data": "adm:set_proxy"}],
                [{"text": "🛠 Open Admin Panel", "callback_data": "adm:home"}],
            ]
        }
        await self.notify_admins(msg, reply_markup=kb)

    # ------------------------------------------------- Force-Subscribe Check
    async def check_force_sub(self, user_id: int) -> list[dict]:
        """Returns list of channels the user still needs to join (empty if all joined)."""
        if self.db.is_admin(user_id):
            return []
        if not self.db.settings.get("force_sub_enabled", True):
            return []
        channels = self.db.settings.get("force_sub_channels") or []
        if not channels:
            return []

        missing = []
        for ch in channels:
            if isinstance(ch, str):
                ch = {"chat_id": ch, "title": ch, "invite_link": ""}
            chat_id = str(ch.get("chat_id") or "").strip()
            if not chat_id:
                continue
            res = await self.api("getChatMember", {"chat_id": chat_id, "user_id": user_id})
            if res and res.get("ok"):
                st = (res.get("result") or {}).get("status", "")
                is_mem = (res.get("result") or {}).get("is_member", False)
                if st in ("creator", "administrator", "member") or (st == "restricted" and is_mem):
                    continue
            else:
                # If bot isn't in channel or chat_id invalid, don't lock out users forever
                desc = (res or {}).get("description", "").lower()
                if "chat not found" in desc or "bot is not a member" in desc:
                    continue
            invite = (ch.get("invite_link") or "").strip()
            if not invite and chat_id.startswith("@"):
                invite = f"https://t.me/{chat_id.lstrip('@')}"
            missing.append({
                "chat_id": chat_id,
                "title": ch.get("title") or chat_id,
                "invite_link": invite or "https://t.me/TheDrunkBots",
            })
        return missing

    def build_fsub_prompt(self, missing: list[dict]) -> tuple[str, dict]:
        brand = self.db.settings.get("brand_name") or DEFAULT_BRAND
        text = (
            f"🔒 • <b>𝗔𝗖𝗖𝗘𝗦𝗦 𝗥𝗘𝗦𝗧𝗥𝗜𝗖𝗧𝗘𝗗 ({esc(brand)})</b>\n\n"
            f"<blockquote>📢 <b>You must join our official channel(s) below to use this bot for free!</b>\n\n"
            f"After joining all channels, tap <b>✅ Verify Membership</b> below.</blockquote>"
        )
        rows = []
        for i, ch in enumerate(missing, 1):
            rows.append([{"text": f"📢 Join {ch['title']}", "url": ch["invite_link"]}])
        rows.append([{"text": "✅ Verify Membership", "callback_data": "usr:verify_fsub"}])
        return text, {"inline_keyboard": rows}

    # ------------------------------------------------- Polling Loop
    async def _poll_loop(self):
        while self.running:
            try:
                res = await self.api(
                    "getUpdates",
                    {
                        "offset": self._offset,
                        "timeout": 25,
                        "allowed_updates": ["message", "callback_query"],
                    },
                    timeout=35.0,
                )
                if not res or not res.get("ok"):
                    await asyncio.sleep(2.0)
                    continue
                for upd in res.get("result") or []:
                    uid = upd.get("update_id", 0)
                    if uid >= self._offset:
                        self._offset = uid + 1
                    asyncio.create_task(self._handle_update(upd))
            except asyncio.CancelledError:
                break
            except Exception as e:
                print("Bot poll error:", e, flush=True)
                await asyncio.sleep(2.0)

    async def _handle_update(self, upd: dict):
        try:
            if "callback_query" in upd:
                await self._on_callback(upd["callback_query"])
            elif "message" in upd:
                await self._on_message(upd["message"])
        except Exception as e:
            print("Update handler error:", e, flush=True)

    # ------------------------------------------------- Message Handler
    async def _on_message(self, msg: dict):
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        from_user = msg.get("from") or {}
        user_id = int(from_user.get("id") or 0)
        msg_id = msg.get("message_id")
        text = (msg.get("text") or msg.get("caption") or "").strip()
        if not chat_id or not user_id:
            return

        user_doc = await self.db.touch_user(from_user)
        is_adm = self.db.is_admin(user_id)

        if user_doc.get("banned") and not is_adm:
            await self.send_message(
                chat_id,
                "🚫 <b>𝗬𝗢𝗨 𝗔𝗥𝗘 𝗕𝗔𝗡𝗡𝗘𝗗</b>\n<blockquote>You have been restricted from using this bot.</blockquote>",
                reply_to=msg_id,
            )
            return

        if self.db.settings.get("maintenance_mode") and not is_adm:
            await self.send_message(
                chat_id,
                "🛠 <b>𝗠𝗔𝗜𝗡𝗧𝗘𝗡𝗔𝗡𝗖𝗘 𝗠𝗢𝗗𝗘</b>\n<blockquote>The bot is currently under scheduled maintenance. Please try again shortly!</blockquote>",
                reply_to=msg_id,
            )
            return

        # Handle interactive admin state prompts
        if is_adm and user_id in self._admin_states and not text.startswith("/"):
            await self._handle_admin_state_input(chat_id, user_id, msg_id, text)
            return

        # Commands
        if text.startswith("/"):
            parts = text.split(maxsplit=1)
            cmd = parts[0].split("@")[0].lower()
            arg = parts[1].strip() if len(parts) > 1 else ""

            if cmd == "/cancel" and is_adm:
                self._admin_states.pop(user_id, None)
                await self.send_message(chat_id, "✅ <b>Cancelled current action.</b>", reply_to=msg_id)
                return

            # Force-sub check for non-admin users on all user commands
            if not is_adm:
                missing = await self.check_force_sub(user_id)
                if missing:
                    f_txt, f_kb = self.build_fsub_prompt(missing)
                    await self.send_message(chat_id, f_txt, reply_to=msg_id, reply_markup=f_kb)
                    return

            if cmd == "/start":
                await self._cmd_start(chat_id, user_id, from_user, msg_id)
                return
            if cmd == "/help":
                await self._cmd_help(chat_id, user_id, msg_id)
                return
            if cmd in ("/status", "/ping"):
                await self._cmd_status(chat_id, user_id, msg_id)
                return
            if cmd == "/myinfo":
                await self._cmd_myinfo(chat_id, user_doc, msg_id)
                return
            if cmd in ("/bypass", "/b", "/dl"):
                if not arg:
                    await self.send_message(
                        chat_id,
                        "⚡ <b>Usage:</b> <code>/bypass https://lksfy.com/...</code>\n"
                        "<blockquote>Or simply send any shortlink directly in the chat!</blockquote>",
                        reply_to=msg_id,
                    )
                    return
                urls = URL_RE.findall(arg) or [arg.split()[0]]
                for u in urls[:3]:
                    await self._process_bypass(chat_id, user_id, from_user, msg_id, u)
                return

            # Admin-only slash commands
            if is_adm:
                if cmd in ("/admin", "/panel", "/settings"):
                    await self._send_admin_panel(chat_id, msg_id)
                    return
                if cmd == "/setproxy":
                    await self._cmd_setproxy(chat_id, msg_id, arg)
                    return
                if cmd == "/testproxy":
                    await self._cmd_testproxy(chat_id, msg_id)
                    return
                if cmd == "/addfsub":
                    await self._cmd_addfsub(chat_id, msg_id, arg)
                    return
                if cmd == "/delfsub":
                    await self._cmd_delfsub(chat_id, msg_id, arg)
                    return
                if cmd == "/fsub":
                    await self._cmd_listfsub(chat_id, msg_id)
                    return
                if cmd == "/clearcache":
                    cnt = await self.db.clear_cache()
                    await self.send_message(
                        chat_id,
                        f"🗑 <b>𝗖𝗔𝗖𝗛𝗘 𝗖𝗟𝗘𝗔𝗥𝗘𝗗</b>\n<blockquote>Removed <code>{cnt}</code> cached links from MongoDB & memory.</blockquote>",
                        reply_to=msg_id,
                    )
                    return
                if cmd == "/delcache":
                    if not arg:
                        await self.send_message(chat_id, "Usage: <code>/delcache https://...</code>", reply_to=msg_id)
                        return
                    ok = await self.db.delete_cache(arg)
                    await self.send_message(
                        chat_id,
                        f"{'✅ Deleted' if ok else '⚠️ Not found in'} cache: <code>{esc(arg)}</code>",
                        reply_to=msg_id,
                    )
                    return
                if cmd == "/logs":
                    await self._cmd_logs(chat_id, msg_id)
                    return
                if cmd == "/clearlogs":
                    cnt = await self.db.clear_error_logs()
                    await self.send_message(
                        chat_id,
                        f"🗑 <b>Cleared {cnt} error logs.</b>",
                        reply_to=msg_id,
                    )
                    return
                if cmd == "/stats":
                    await self._cmd_status(chat_id, user_id, msg_id)
                    return
                if cmd == "/ban" and arg:
                    try:
                        await self.db.set_user_banned(int(arg), True)
                        await self.send_message(chat_id, f"🚫 Banned user <code>{esc(arg)}</code>", reply_to=msg_id)
                    except Exception:
                        await self.send_message(chat_id, "Usage: <code>/ban user_id</code>", reply_to=msg_id)
                    return
                if cmd == "/unban" and arg:
                    try:
                        await self.db.set_user_banned(int(arg), False)
                        await self.send_message(chat_id, f"✅ Unbanned user <code>{esc(arg)}</code>", reply_to=msg_id)
                    except Exception:
                        await self.send_message(chat_id, "Usage: <code>/unban user_id</code>", reply_to=msg_id)
                    return
                if cmd == "/addadmin" and arg:
                    try:
                        aid = int(arg)
                        admins = list(self.db.settings.get("admin_ids") or [])
                        if aid not in admins:
                            admins.append(aid)
                        await self.db.update_settings({"admin_ids": admins})
                        await self.send_message(chat_id, f"✅ Added admin <code>{aid}</code>", reply_to=msg_id)
                    except Exception:
                        await self.send_message(chat_id, "Usage: <code>/addadmin user_id</code>", reply_to=msg_id)
                    return
                if cmd == "/deladmin" and arg:
                    try:
                        aid = int(arg)
                        admins = [x for x in (self.db.settings.get("admin_ids") or []) if int(x) != aid]
                        await self.db.update_settings({"admin_ids": admins})
                        await self.send_message(chat_id, f"🗑 Removed admin <code>{aid}</code>", reply_to=msg_id)
                    except Exception:
                        await self.send_message(chat_id, "Usage: <code>/deladmin user_id</code>", reply_to=msg_id)
                    return
                if cmd == "/broadcast":
                    reply_msg = msg.get("reply_to_message")
                    b_text = arg or ((reply_msg or {}).get("text") or "")
                    if not b_text:
                        await self.send_message(
                            chat_id,
                            "📢 <b>Usage:</b> <code>/broadcast Your message here</code> (or reply to a message with <code>/broadcast</code>)",
                            reply_to=msg_id,
                        )
                        return
                    await self._run_broadcast(chat_id, msg_id, b_text)
                    return

        # Direct link sent in chat
        urls = URL_RE.findall(text)
        if urls:
            if not is_adm:
                missing = await self.check_force_sub(user_id)
                if missing:
                    f_txt, f_kb = self.build_fsub_prompt(missing)
                    await self.send_message(chat_id, f_txt, reply_to=msg_id, reply_markup=f_kb)
                    return
            for u in urls[:3]:
                await self._process_bypass(chat_id, user_id, from_user, msg_id, u)

    # ------------------------------------------------- Bypass Flow (Screenshots UI)
    async def _process_bypass(self, chat_id: int, user_id: int, from_user: dict, reply_to: int, raw_url: str):
        url = norm_url(raw_url)
        await self.db.touch_user(from_user, increment_bypass=True)

        # 1. Check MongoDB / Memory Cloud Cache first!
        hit = await self.db.get_cache(url, force=False)
        if hit and hit.get("url"):
            dest_url = hit["url"]
            msg_text = format_success_message(raw_url, dest_url, 0.01, cached=True)
            kb = {
                "inline_keyboard": [
                    [
                        {"text": "COPY LINK 📋", "copy_text": {"text": dest_url}},
                        {"text": "OPEN LINK ↗️", "url": dest_url},
                    ]
                ]
            }
            await self.send_message(chat_id, msg_text, reply_to=reply_to, reply_markup=kb)
            return

        # 2. Send initial live progress message (matches image-2.png!)
        now_m = time.monotonic()
        base_est = estimate_duration(url)
        queue_wait = 0.0
        for j_uid, (j_t0, j_est) in list(self._active_jobs.items()):
            # If the same URL is already in-flight, share its remaining time rather than adding to queue
            if j_uid.startswith(url + "|"):
                rem_same = max(4.0, j_est - (now_m - j_t0))
                base_est = rem_same
                queue_wait = 0.0
                break
            queue_wait += max(5.0, j_est - (now_m - j_t0))
        est_total = base_est + queue_wait
        job_track_id = f"{url}|{chat_id}:{reply_to}:{now_m}"
        self._active_jobs[job_track_id] = [now_m, base_est]

        init_text = format_fetching_message(5, 0.4, est_total)
        sent = await self.send_message(chat_id, init_text, reply_to=reply_to)
        prog_msg_id = ((sent or {}).get("result") or {}).get("message_id")

        t0 = time.monotonic()
        step_state: dict[str, Any] = {
            "pct_floor": 5,
            "done": False,
            "started_at": None,
            "reached_final": False,
            "rem_at": None,  # (monotonic_ts, seconds_remaining)
        }
        log_lines: list[str] = []

        def on_log(line: str):
            now_l = time.monotonic()
            low = line.lower()
            if step_state["started_at"] is None and ("opening " in low or "→ " in low):
                step_state["started_at"] = now_l
                self._active_jobs[job_track_id] = [now_l, base_est]
                step_state["pct_floor"] = max(step_state["pct_floor"], 12)
            elapsed_now = now_l - (step_state["started_at"] or t0)
            log_lines.append(f"[{elapsed_now:5.1f}s] {line}")

            m_wait = re.search(
                r"waiting\s+(\d+)s\b|server\s+timer\s+(\d+)s\s+left",
                low,
            )
            if m_wait:
                sec = float(m_wait.group(1) or m_wait.group(2) or 0)
                if sec > 0:
                    extra = 9.0 if "arolinks" in low else 12.0
                    step_state["rem_at"] = (now_l, sec + extra)
                    self._active_jobs[job_track_id] = [now_l, sec + extra]

            if "reached the shortener's final page" in low:
                step_state["reached_final"] = True
                step_state["pct_floor"] = max(step_state["pct_floor"], 86)
                step_state["rem_at"] = (now_l, 6.0)
                self._active_jobs[job_track_id] = [now_l, 6.0]
            elif "following final redirect" in low or "links/go api" in low:
                step_state["reached_final"] = True
                step_state["pct_floor"] = max(step_state["pct_floor"], 95)
                step_state["rem_at"] = (now_l, 2.0)
            elif "final link obtained" in low or "final link found" in low or "jumping to safelink" in low:
                step_state["pct_floor"] = max(step_state["pct_floor"], 72)
                step_state["rem_at"] = (now_l, 11.0)
            elif "turnstile solved" in low:
                step_state["pct_floor"] = max(step_state["pct_floor"], 64)
            elif "submitting safelink form" in low or "click" in low or "shortcut" in low:
                step_state["pct_floor"] = min(78, step_state["pct_floor"] + 10)

        async def updater():
            last_pct = 5
            while not step_state["done"] and prog_msg_id:
                await asyncio.sleep(2.4)
                if step_state["done"]:
                    break
                now_u = time.monotonic()
                el = now_u - t0
                started_at = step_state["started_at"]
                if started_at is None:
                    # Still waiting in browser queue
                    pct = min(12, last_pct + 1)
                    rem = max(base_est, est_total - el)
                else:
                    active_el = now_u - started_at
                    if step_state["rem_at"] is not None:
                        r_t0, r_sec = step_state["rem_at"]
                        rem = max(3.0 if step_state["reached_final"] else 8.0, r_sec - (now_u - r_t0))
                        total_proj = active_el + rem
                        time_pct = int(min(92, (active_el / max(8.0, total_proj)) * 90))
                    else:
                        rem = max(3.0 if step_state["reached_final"] else 9.0, base_est - active_el)
                        time_pct = int(min(88, (active_el / max(8.0, base_est)) * 84))
                    cap = 96 if step_state["reached_final"] else 84
                    pct = max(last_pct + 1, time_pct, step_state["pct_floor"])
                    pct = min(cap, pct)
                last_pct = pct
                try:
                    await self.edit_message(chat_id, prog_msg_id, format_fetching_message(pct, el, rem))
                except Exception:
                    pass

        upd_task = asyncio.create_task(updater())
        try:
            res = await self.resolve_fn(
                url,
                force=False,
                origin="telegram",
                user_id=user_id,
                username=from_user.get("username"),
                extra_log_cb=on_log,
            )
            step_state["done"] = True
            upd_task.cancel()
            self._active_jobs.pop(job_track_id, None)

            dest_url = res["url"]
            elapsed = float(res.get("elapsed") or round(time.monotonic() - t0, 2))
            is_cached = bool(res.get("cached"))
            final_text = format_success_message(raw_url, dest_url, elapsed, cached=is_cached)
            kb = {
                "inline_keyboard": [
                    [
                        {"text": "COPY LINK 📋", "copy_text": {"text": dest_url}},
                        {"text": "OPEN LINK ↗️", "url": dest_url},
                    ]
                ]
            }
            if prog_msg_id:
                await self.edit_message(chat_id, prog_msg_id, final_text, reply_markup=kb)
            else:
                await self.send_message(chat_id, final_text, reply_to=reply_to, reply_markup=kb)
        except Exception:
            step_state["done"] = True
            upd_task.cancel()
            self._active_jobs.pop(job_track_id, None)
            err_text = format_user_error_message(raw_url)
            if prog_msg_id:
                await self.edit_message(chat_id, prog_msg_id, err_text)
            else:
                await self.send_message(chat_id, err_text, reply_to=reply_to)

    # ------------------------------------------------- User Commands
    async def _cmd_start(self, chat_id: int, user_id: int, from_user: dict, msg_id: int | None):
        brand = self.db.settings.get("brand_name") or DEFAULT_BRAND
        photo = (self.db.settings.get("start_photo") or DEFAULT_START_PHOTO).strip()
        tpl = self.db.settings.get("welcome_message") or ""
        first_name = esc(from_user.get("first_name") or "User")
        body = tpl.replace("{first_name}", first_name).replace("{brand}", esc(brand))
        caption = (
            f"🍹 • <b>𝗪𝗘𝗟𝗖𝗢𝗠𝗘 𝗧𝗢 {esc(brand.upper())}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{body}"
        )
        upd_chan = (self.db.settings.get("updates_channel") or "https://t.me/TheDrunkBots").strip()
        if upd_chan.startswith("@"):
            upd_chan = f"https://t.me/{upd_chan.lstrip('@')}"
        elif not upd_chan.startswith("http"):
            upd_chan = "https://t.me/TheDrunkBots"

        rows = [
            [
                {"text": "⚡ How To Use", "callback_data": "usr:help"},
                {"text": "📊 Bot Status", "callback_data": "usr:status"},
            ],
            [
                {"text": "👤 My Profile", "callback_data": "usr:myinfo"},
                {"text": "📢 Updates Channel", "url": upd_chan},
            ],
        ]
        if self.db.is_admin(user_id):
            rows.append([{"text": "🛠 Admin Control Panel", "callback_data": "adm:home"}])

        await self.send_photo(chat_id, photo, caption, reply_to=msg_id, reply_markup={"inline_keyboard": rows})

    async def _cmd_help(self, chat_id: int, user_id: int, msg_id: int | None):
        brand = self.db.settings.get("brand_name") or DEFAULT_BRAND
        txt = (
            f"📖 • <b>𝗛𝗢𝗪 𝗧𝗢 𝗨𝗦𝗘 ({esc(brand)})</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<blockquote>1️⃣ <b>Send any shortlink</b> directly in this chat (or use <code>/bypass &lt;link&gt;</code>).\n"
            f"2️⃣ Watch the <b>live progress bar &amp; ETA</b> while our cloud browser bypasses ads, timers &amp; captchas.\n"
            f"3️⃣ Receive your <b>direct destination link</b> with one-tap <b>COPY LINK 📋</b> &amp; <b>OPEN LINK ↗️</b> buttons!</blockquote>\n\n"
            f"🔗 <b>𝗦𝗨𝗣𝗣𝗢𝗥𝗧𝗘𝗗 𝗦𝗛𝗢𝗥𝗧𝗘𝗡𝗘𝗥𝗦 :</b>\n"
            f"<blockquote>• <code>lksfy.com</code> • <code>get2short.com</code> • <code>arolinks.com</code>\n"
            f"• <code>shortwala.com</code> • <code>softurl.in</code> • <code>earnlinks.in</code>\n"
            f"• <code>babylinks.in</code> • <code>liteshort.com</code> • <code>urllinkshort.in</code>\n"
            f"• <code>vplink.in</code> • <code>linksfire.co</code> • <code>alpha-links.in</code>\n"
            f"• <code>nowshort.com</code> • <code>linksflys.com</code> &amp; all WP Safelink blogs!</blockquote>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🏷 <b>𝗣𝗢𝗪𝗘𝗥𝗘𝗗 𝗕𝗬 :</b> <code>{esc(brand)}</code>"
        )
        rows = [[{"text": "🔙 Back to Home", "callback_data": "usr:start"}]]
        if self.db.is_admin(user_id):
            rows.append([{"text": "🛠 Admin Control Panel", "callback_data": "adm:home"}])
        await self.send_message(chat_id, txt, reply_to=msg_id, reply_markup={"inline_keyboard": rows})

    async def _cmd_status(self, chat_id: int, user_id: int, msg_id: int | None):
        brand = self.db.settings.get("brand_name") or DEFAULT_BRAND
        st = self.db.settings.get("stats") or {}
        cache_count = len(self.db.local_cache)
        users_count = len(self.db.local_users)
        px_on = bool(self.db.settings.get("proxy"))
        px_exp = bool(self.db.settings.get("proxy_expired"))
        px_status = "🟢 Active Tunnel" if (px_on and not px_exp) else ("🔴 Expired Tunnel" if px_exp else "🟡 Direct Connection")
        mongo_status = "🟢 Connected (Atlas)" if self.db.connected else "🟡 Local Fallback"
        cache_status = "🟢 Enabled" if self.db.settings.get("cache_enabled", True) else "🔴 Disabled"

        txt = (
            f"📊 • <b>𝗦𝗬𝗦𝗧𝗘𝗠 𝗦𝗧𝗔𝗧𝗨𝗦 ({esc(brand)})</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<blockquote>⚡ <b>𝗕𝗢𝗧 𝗦𝗧𝗔𝗧𝗨𝗦 :</b> <code>ONLINE</code>\n"
            f"🗄 <b>𝗠𝗢𝗡𝗚𝗢 𝗗𝗕 :</b> <code>{mongo_status}</code>\n"
            f"🚀 <b>𝗖𝗟𝗢𝗨𝗗 𝗖𝗔𝗖𝗛𝗘 :</b> <code>{cache_status} ({cache_count} links)</code>\n"
            f"✅ <b>𝗧𝗢𝗧𝗔𝗟 𝗕𝗬𝗣𝗔𝗦𝗦𝗘𝗗 :</b> <code>{st.get('total_bypasses', 0)}</code>\n"
            f"⚡ <b>𝗖𝗔𝗖𝗛𝗘 𝗛𝗜𝗧𝗦 :</b> <code>{st.get('cache_hits', 0)}</code>\n"
            f"👥 <b>𝗧𝗢𝗧𝗔𝗟 𝗨𝗦𝗘𝗥𝗦 :</b> <code>{users_count}</code></blockquote>"
        )
        if self.db.is_admin(user_id):
            txt += (
                f"\n🔐 <b>𝗔𝗗𝗠𝗜𝗡 𝗗𝗜𝗔𝗚𝗡𝗢𝗦𝗧𝗜𝗖𝗦 :</b>\n"
                f"<blockquote>🌐 <b>𝗧𝗨𝗡𝗡𝗘𝗟 :</b> <code>{px_status}</code>\n"
                f"❌ <b>𝗘𝗥𝗥𝗢𝗥 𝗟𝗢𝗚𝗦 :</b> <code>{len(self.db.local_logs)}</code>\n"
                f"📢 <b>𝗙𝗢𝗥𝗖𝗘 𝗦𝗨𝗕 :</b> <code>{len(self.db.settings.get('force_sub_channels') or [])} channel(s)</code></blockquote>"
            )
        await self.send_message(chat_id, txt, reply_to=msg_id)

    async def _cmd_myinfo(self, chat_id: int, user_doc: dict, msg_id: int | None):
        uid = user_doc.get("user_id")
        role = "👑 Owner / Admin" if self.db.is_admin(uid) else "👤 Member"
        txt = (
            f"👤 • <b>𝗬𝗢𝗨𝗥 𝗣𝗥𝗢𝗙𝗜𝗟𝗘</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<blockquote>🆔 <b>𝗨𝗦𝗘𝗥 𝗜𝗗 :</b> <code>{uid}</code>\n"
            f"🏷 <b>𝗡𝗔𝗠𝗘 :</b> <code>{esc(user_doc.get('first_name') or 'User')}</code>\n"
            f"🎖 <b>𝗥𝗢𝗟𝗘 :</b> <code>{role}</code>\n"
            f"🔗 <b>𝗟𝗜𝗡𝗞𝗦 𝗕𝗬𝗣𝗔𝗦𝗦𝗘𝗗 :</b> <code>{user_doc.get('bypass_count', 0)}</code></blockquote>"
        )
        await self.send_message(chat_id, txt, reply_to=msg_id)

    # ------------------------------------------------- Admin Commands & Panel
    def _build_admin_panel_view(self) -> tuple[str, dict]:
        s = self.db.settings
        px = s.get("proxy")
        masked_px = re.sub(r"(://[^:/@]+:)[^@]+@", r"\1•••@", px) if px else "None (Direct)"
        px_badge = "🟢 Active" if (px and not s.get("proxy_expired")) else ("🔴 Expired" if s.get("proxy_expired") else "⚪ Direct")
        fsub_on = "🟢 ON" if s.get("force_sub_enabled", True) else "🔴 OFF"
        fsub_cnt = len(s.get("force_sub_channels") or [])
        cache_on = "🟢 ON" if s.get("cache_enabled", True) else "🔴 OFF"
        maint_on = "🟡 ON" if s.get("maintenance_mode") else "🟢 OFF"
        st = s.get("stats") or {}

        text = (
            f"🛠 • <b>𝗧𝗛𝗘𝗗𝗥𝗨𝗡𝗞𝗕𝗢𝗧𝗦 𝗔𝗗𝗠𝗜𝗡 𝗣𝗔𝗡𝗘𝗟</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🌐 <b>𝗧𝗨𝗡𝗡𝗘𝗟 / 𝗣𝗥𝗢𝗫𝗬 ({px_badge}) :</b>\n"
            f"<blockquote><code>{esc(masked_px)}</code></blockquote>\n"
            f"📊 <b>𝗟𝗜𝗩𝗘 𝗠𝗘𝗧𝗥𝗜𝗖𝗦 &amp; 𝗦𝗬𝗡𝗖 :</b>\n"
            f"<blockquote>• 🗄 <b>MongoDB :</b> <code>{'Connected' if self.db.connected else 'Local Fallback'}</code>\n"
            f"• 🚀 <b>Cache :</b> <code>{cache_on} ({len(self.db.local_cache)} items, TTL {s.get('cache_hours', 24)}h)</code>\n"
            f"• 📢 <b>Force-Sub :</b> <code>{fsub_on} ({fsub_cnt} channels)</code>\n"
            f"• 👥 <b>Users :</b> <code>{len(self.db.local_users)}</code>  •  ✅ <b>Bypassed :</b> <code>{st.get('total_bypasses', 0)}</code>\n"
            f"• 🚨 <b>Error Logs :</b> <code>{len(self.db.local_logs)}</code>  •  🛠 <b>Maint :</b> <code>{maint_on}</code></blockquote>\n"
            f"<i>All changes sync instantaneously with MongoDB &amp; Web Admin UI.</i>"
        )
        kb = {
            "inline_keyboard": [
                [
                    {"text": "🌐 Set Tunnel Proxy", "callback_data": "adm:set_proxy"},
                    {"text": "🔍 Test Exit IP", "callback_data": "adm:test_proxy"},
                ],
                [
                    {"text": "🗑 Clear Proxy (Direct)", "callback_data": "adm:clear_proxy"},
                    {"text": f"🚀 Cache: {cache_on}", "callback_data": "adm:toggle_cache"},
                ],
                [
                    {"text": f"📢 Force-Sub ({fsub_cnt})", "callback_data": "adm:fsub_menu"},
                    {"text": "🗄 Cache Manager", "callback_data": "adm:cache_menu"},
                ],
                [
                    {"text": f"🚨 Error Logs ({len(self.db.local_logs)})", "callback_data": "adm:logs_menu"},
                    {"text": f"👥 Users & Broadcast ({len(self.db.local_users)})", "callback_data": "adm:users_menu"},
                ],
                [
                    {"text": "⚙️ Bot & Engine Settings", "callback_data": "adm:settings_menu"},
                    {"text": "🔄 Refresh Panel", "callback_data": "adm:home"},
                ],
            ]
        }
        return text, kb

    async def _send_admin_panel(self, chat_id: int, msg_id: int | None = None, edit_id: int | None = None):
        text, kb = self._build_admin_panel_view()
        if edit_id:
            await self.edit_message(chat_id, edit_id, text, reply_markup=kb)
        else:
            await self.send_message(chat_id, text, reply_to=msg_id, reply_markup=kb)

    async def _cmd_setproxy(self, chat_id: int, msg_id: int | None, arg: str):
        if not arg:
            await self.send_message(
                chat_id,
                "🌐 <b>Usage:</b> <code>/setproxy http://user:pass@host:port</code>\n"
                "Or <code>/setproxy off</code> to use direct connection.",
                reply_to=msg_id,
            )
            return
        new_px = None if arg.lower() in ("off", "none", "clear", "direct") else arg.strip()
        wait_msg = await self.send_message(chat_id, "⏳ <b>Applying tunnel proxy &amp; restarting browser context…</b>", reply_to=msg_id)
        wid = ((wait_msg or {}).get("result") or {}).get("message_id")
        try:
            await self.set_proxy_fn(new_px)
            info = await self.proxy_test_fn()
            if info.get("ok"):
                ip_txt = f"{info.get('ip')} ({info.get('city') or ''}, {info.get('country') or ''}) — {info.get('org') or ''}"
                status_line = f"✅ <b>𝗧𝗨𝗡𝗡𝗘𝗟 𝗨𝗣𝗗𝗔𝗧𝗘𝗗 &amp; 𝗩𝗘𝗥𝗜𝗙𝗜𝗘𝗗</b>\n<blockquote>🌐 <b>Exit IP:</b> <code>{esc(ip_txt)}</code></blockquote>"
            else:
                status_line = f"⚠️ <b>𝗧𝗨𝗡𝗡𝗘𝗟 𝗦𝗔𝗩𝗘𝗗 (𝗧𝗘𝗦𝗧 𝗙𝗔𝗜𝗟𝗘𝗗)</b>\n<blockquote><code>{esc(info.get('error'))}</code></blockquote>"
        except Exception as e:
            status_line = f"❌ <b>𝗙𝗔𝗜𝗟𝗘𝗗 𝗧𝗢 𝗦𝗘𝗧 𝗣𝗥𝗢𝗫𝗬 :</b> <code>{esc(e)}</code>"
        if wid:
            await self.edit_message(chat_id, wid, status_line)
        else:
            await self.send_message(chat_id, status_line, reply_to=msg_id)

    async def _cmd_testproxy(self, chat_id: int, msg_id: int | None):
        wait_msg = await self.send_message(chat_id, "🔍 <b>Testing current exit IP…</b>", reply_to=msg_id)
        wid = ((wait_msg or {}).get("result") or {}).get("message_id")
        info = await self.proxy_test_fn()
        if info.get("ok"):
            kind = "⚠️ Datacenter IP" if info.get("datacenter") else "✅ Residential / Home IP"
            txt = (
                f"🌐 • <b>𝗘𝗫𝗜𝗧 𝗜𝗣 𝗧𝗘𝗦𝗧 𝗥𝗘𝗦𝗨𝗟𝗧</b>\n\n"
                f"<blockquote>• <b>Via Proxy :</b> <code>{info.get('via_proxy')}</code>\n"
                f"• <b>Exit IP :</b> <code>{esc(info.get('ip'))}</code>\n"
                f"• <b>Location :</b> <code>{esc(info.get('city'))}, {esc(info.get('region'))}, {esc(info.get('country'))}</code>\n"
                f"• <b>ISP / Org :</b> <code>{esc(info.get('org'))}</code>\n"
                f"• <b>Type :</b> <code>{kind}</code></blockquote>"
            )
        else:
            txt = f"❌ <b>𝗣𝗥𝗢𝗫𝗬 𝗧𝗘𝗦𝗧 𝗙𝗔𝗜𝗟𝗘𝗗 :</b>\n<blockquote><code>{esc(info.get('error'))}</code></blockquote>"
        if wid:
            await self.edit_message(chat_id, wid, txt)
        else:
            await self.send_message(chat_id, txt, reply_to=msg_id)

    async def _cmd_addfsub(self, chat_id: int, msg_id: int | None, arg: str):
        if not arg:
            await self.send_message(
                chat_id,
                "📢 <b>Usage:</b> <code>/addfsub @ChannelUsername</code> or <code>/addfsub -1001234567890 https://t.me/+inviteLink Channel Title</code>",
                reply_to=msg_id,
            )
            return
        parts = arg.split()
        cid = parts[0].strip()
        invite = parts[1].strip() if len(parts) > 1 and parts[1].startswith("http") else ""
        title = " ".join(parts[2:]) if len(parts) > 2 else (parts[1] if len(parts) > 1 and not invite else cid)
        if not invite and cid.startswith("@"):
            invite = f"https://t.me/{cid.lstrip('@')}"
        channels = list(self.db.settings.get("force_sub_channels") or [])
        channels = [c for c in channels if str(c.get("chat_id")).lower() != cid.lower()]
        channels.append({"chat_id": cid, "title": title or cid, "invite_link": invite})
        await self.db.update_settings({"force_sub_channels": channels})
        await self.send_message(
            chat_id,
            f"✅ <b>Added Force-Sub Channel:</b> <code>{esc(cid)}</code> ({esc(title)})",
            reply_to=msg_id,
        )

    async def _cmd_delfsub(self, chat_id: int, msg_id: int | None, arg: str):
        if not arg:
            await self.send_message(chat_id, "Usage: <code>/delfsub @ChannelUsername</code>", reply_to=msg_id)
            return
        cid = arg.strip().lower()
        channels = list(self.db.settings.get("force_sub_channels") or [])
        new_ch = [c for c in channels if str(c.get("chat_id", "")).lower() != cid]
        await self.db.update_settings({"force_sub_channels": new_ch})
        await self.send_message(chat_id, f"🗑 <b>Removed Force-Sub Channel:</b> <code>{esc(arg)}</code>", reply_to=msg_id)

    async def _cmd_listfsub(self, chat_id: int, msg_id: int | None, edit_id: int | None = None):
        channels = self.db.settings.get("force_sub_channels") or []
        enabled = self.db.settings.get("force_sub_enabled", True)
        lines = []
        rows = [
            [
                {"text": f"Toggle Force-Sub ({'🟢 ON' if enabled else '🔴 OFF'})", "callback_data": "adm:toggle_fsub"},
                {"text": "➕ Add Channel", "callback_data": "adm:add_fsub_prompt"},
            ]
        ]
        for i, c in enumerate(channels):
            cid = c.get("chat_id", "")
            title = c.get("title") or cid
            inv = c.get("invite_link") or ""
            lines.append(f"{i+1}. <b>{esc(title)}</b> (<code>{esc(cid)}</code>)\n   🔗 {esc(inv)}")
            rows.append([{"text": f"🗑 Remove {title}", "callback_data": f"adm:del_fsub:{cid}"}])
        rows.append([{"text": "🔙 Back to Admin Panel", "callback_data": "adm:home"}])
        body = "\n".join(lines) if lines else "No Force-Sub channels configured yet."
        txt = (
            f"📢 • <b>𝗙𝗢𝗥𝗖𝗘-𝗦𝗨𝗕𝗦𝗖𝗥𝗜𝗕𝗘 𝗠𝗔𝗡𝗔𝗚𝗘𝗥</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"• <b>Status :</b> <code>{'ENABLED' if enabled else 'DISABLED'}</code>\n\n"
            f"<blockquote>{body}</blockquote>\n"
            f"<i>Note: Make sure the bot is an Admin in each Force-Sub channel so it can verify members.</i>"
        )
        if edit_id:
            await self.edit_message(chat_id, edit_id, txt, reply_markup={"inline_keyboard": rows})
        else:
            await self.send_message(chat_id, txt, reply_to=msg_id, reply_markup={"inline_keyboard": rows})

    async def _cmd_logs(self, chat_id: int, msg_id: int | None = None, edit_id: int | None = None):
        logs = await self.db.list_error_logs(limit=6)
        if not logs:
            txt = "🚨 • <b>𝗘𝗥𝗥𝗢𝗥 𝗟𝗢𝗚𝗦</b>\n\n<blockquote>✅ No error logs recorded! Everything is running smoothly.</blockquote>"
        else:
            parts = []
            for item in logs:
                ts = time.strftime("%H:%M:%S", time.localtime(item.get("at", 0)))
                parts.append(
                    f"• <code>[{ts}]</code> <b>{esc(item.get('source'))}</b>\n"
                    f"  ❌ <code>{esc((item.get('error') or '')[:120])}</code>"
                )
            txt = (
                f"🚨 • <b>𝗥𝗘𝗖𝗘𝗡𝗧 𝗘𝗥𝗥𝗢𝗥 𝗟𝗢𝗚𝗦 ({len(self.db.local_logs)})</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"<blockquote>" + "\n\n".join(parts) + "</blockquote>"
            )
        kb = {
            "inline_keyboard": [
                [{"text": "🗑 Clear All Error Logs", "callback_data": "adm:clear_logs"}],
                [{"text": "🔙 Back to Admin Panel", "callback_data": "adm:home"}],
            ]
        }
        if edit_id:
            await self.edit_message(chat_id, edit_id, txt, reply_markup=kb)
        else:
            await self.send_message(chat_id, txt, reply_to=msg_id, reply_markup=kb)

    async def _run_broadcast(self, chat_id: int, msg_id: int | None, text: str):
        uids = await self.db.get_all_user_ids(include_banned=False)
        status_msg = await self.send_message(
            chat_id,
            f"📢 <b>Broadcasting message to <code>{len(uids)}</code> users…</b>",
            reply_to=msg_id,
        )
        sid = ((status_msg or {}).get("result") or {}).get("message_id")
        sent_ok = 0
        failed = 0
        formatted = f"📢 • <b>𝗔𝗡𝗡𝗢𝗨𝗡𝗖𝗘𝗠𝗘𝗡𝗧</b>\n━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>{text}</blockquote>"
        for uid in uids:
            r = await self.send_message(uid, formatted)
            if r and r.get("ok"):
                sent_ok += 1
            else:
                failed += 1
            await asyncio.sleep(0.05)
        done_txt = (
            f"✅ • <b>𝗕𝗥𝗢𝗔𝗗𝗖𝗔𝗦𝗧 𝗖𝗢𝗠𝗣𝗟𝗘𝗧𝗘</b>\n\n"
            f"<blockquote>• <b>Delivered :</b> <code>{sent_ok}</code>\n"
            f"• <b>Failed / Blocked :</b> <code>{failed}</code></blockquote>"
        )
        if sid:
            await self.edit_message(chat_id, sid, done_txt)
        else:
            await self.send_message(chat_id, done_txt, reply_to=msg_id)

    # ------------------------------------------------- Interactive Admin Input
    async def _handle_admin_state_input(self, chat_id: int, user_id: int, msg_id: int, text: str):
        state = self._admin_states.pop(user_id, None)
        if not state:
            return
        action = state.get("action")
        if action == "set_proxy":
            await self._cmd_setproxy(chat_id, msg_id, text)
        elif action == "add_fsub":
            await self._cmd_addfsub(chat_id, msg_id, text)
        elif action == "add_cache":
            parts = [p.strip() for p in text.split("|") if p.strip()]
            if len(parts) < 2:
                parts = text.split()
            if len(parts) >= 2:
                entry = await self.db.set_cache(parts[0], {"url": parts[1], "method": "admin"})
                await self.send_message(
                    chat_id,
                    f"✅ <b>Saved to Cloud Cache:</b>\n<blockquote><code>{esc(entry['source'])}</code>\n➡️ <code>{esc(entry['url'])}</code></blockquote>",
                    reply_to=msg_id,
                )
            else:
                await self.send_message(chat_id, "⚠️ Invalid format. Send: <code>shortlink | destination_url</code>", reply_to=msg_id)
        elif action == "del_cache":
            ok = await self.db.delete_cache(text.strip())
            await self.send_message(
                chat_id,
                f"{'✅ Deleted from cache:' if ok else '⚠️ Not found in cache:'} <code>{esc(text)}</code>",
                reply_to=msg_id,
            )
        elif action == "broadcast":
            await self._run_broadcast(chat_id, msg_id, text)
        elif action == "ban_user":
            try:
                uid = int(text.strip())
                await self.db.set_user_banned(uid, True)
                await self.send_message(chat_id, f"🚫 <b>Banned user:</b> <code>{uid}</code>", reply_to=msg_id)
            except Exception:
                await self.send_message(chat_id, "⚠️ Invalid User ID.", reply_to=msg_id)
        elif action == "unban_user":
            try:
                uid = int(text.strip())
                await self.db.set_user_banned(uid, False)
                await self.send_message(chat_id, f"✅ <b>Unbanned user:</b> <code>{uid}</code>", reply_to=msg_id)
            except Exception:
                await self.send_message(chat_id, "⚠️ Invalid User ID.", reply_to=msg_id)
        elif action == "set_welcome":
            await self.db.update_settings({"welcome_message": text})
            await self.send_message(chat_id, "✅ <b>Updated Welcome Message!</b>", reply_to=msg_id)
        elif action == "set_photo":
            await self.db.update_settings({"start_photo": text.strip()})
            await self.send_message(chat_id, f"✅ <b>Updated Start Photo URL:</b>\n<code>{esc(text.strip())}</code>", reply_to=msg_id)
        elif action == "set_cache_hours":
            try:
                h = float(text.strip())
                await self.db.update_settings({"cache_hours": h})
                await self.send_message(chat_id, f"✅ <b>Cache TTL updated to <code>{h}</code> hours.</b>", reply_to=msg_id)
            except Exception:
                await self.send_message(chat_id, "⚠️ Invalid number.", reply_to=msg_id)

    # ------------------------------------------------- Callback Query Handler
    async def _on_callback(self, cb: dict):
        cb_id = cb.get("id")
        data = cb.get("data") or ""
        from_user = cb.get("from") or {}
        user_id = int(from_user.get("id") or 0)
        msg = cb.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        msg_id = msg.get("message_id")
        if not chat_id or not user_id:
            return

        if data == "usr:verify_fsub":
            missing = await self.check_force_sub(user_id)
            if missing:
                await self.api(
                    "answerCallbackQuery",
                    {"callback_query_id": cb_id, "text": "❌ You haven't joined all required channels yet!", "show_alert": True},
                )
                return
            await self.api(
                "answerCallbackQuery",
                {"callback_query_id": cb_id, "text": "✅ Membership verified! Welcome to TheDrunkBots.", "show_alert": False},
            )
            await self._cmd_start(chat_id, user_id, from_user, None)
            return

        if data == "usr:help":
            await self.api("answerCallbackQuery", {"callback_query_id": cb_id})
            await self._cmd_help(chat_id, user_id, None)
            return

        if data == "usr:status":
            await self.api("answerCallbackQuery", {"callback_query_id": cb_id})
            await self._cmd_status(chat_id, user_id, None)
            return

        if data == "usr:myinfo":
            await self.api("answerCallbackQuery", {"callback_query_id": cb_id})
            udoc = await self.db.touch_user(from_user)
            await self._cmd_myinfo(chat_id, udoc, None)
            return

        if data == "usr:start":
            await self.api("answerCallbackQuery", {"callback_query_id": cb_id})
            await self._cmd_start(chat_id, user_id, from_user, None)
            return

        # Admin callbacks
        if data.startswith("adm:"):
            if not self.db.is_admin(user_id):
                await self.api(
                    "answerCallbackQuery",
                    {"callback_query_id": cb_id, "text": "⛔ Admin access only!", "show_alert": True},
                )
                return
            await self.api("answerCallbackQuery", {"callback_query_id": cb_id})
            has_text = bool(msg.get("text"))
            edit_target = msg_id if has_text else None

            if data == "adm:home":
                self._admin_states.pop(user_id, None)
                await self._send_admin_panel(chat_id, edit_id=edit_target)
                return

            if data == "adm:set_proxy":
                self._admin_states[user_id] = {"action": "set_proxy"}
                txt = (
                    "🌐 • <b>𝗦𝗘𝗧 𝗧𝗨𝗡𝗡𝗘𝗟 / 𝗣𝗥𝗢𝗫𝗬</b>\n\n"
                    "<blockquote>Send the new Pinggy / residential proxy URL now in the format:\n"
                    "<code>http://user:pass@host:port</code>\n\n"
                    "Or send <code>off</code> for direct connection.\n"
                    "Send /cancel to abort.</blockquote>"
                )
                kb = {"inline_keyboard": [[{"text": "🔙 Cancel", "callback_data": "adm:home"}]]}
                if edit_target:
                    await self.edit_message(chat_id, edit_target, txt, reply_markup=kb)
                else:
                    await self.send_message(chat_id, txt, reply_markup=kb)
                return

            if data == "adm:clear_proxy":
                await self.set_proxy_fn(None)
                await self._send_admin_panel(chat_id, edit_id=edit_target)
                return

            if data == "adm:test_proxy":
                await self._cmd_testproxy(chat_id, None)
                return

            if data == "adm:toggle_cache":
                cur = bool(self.db.settings.get("cache_enabled", True))
                await self.db.update_settings({"cache_enabled": not cur})
                await self._send_admin_panel(chat_id, edit_id=edit_target)
                return

            if data == "adm:fsub_menu":
                await self._cmd_listfsub(chat_id, None, edit_id=edit_target)
                return

            if data == "adm:toggle_fsub":
                cur = bool(self.db.settings.get("force_sub_enabled", True))
                await self.db.update_settings({"force_sub_enabled": not cur})
                await self._cmd_listfsub(chat_id, None, edit_id=edit_target)
                return

            if data == "adm:add_fsub_prompt":
                self._admin_states[user_id] = {"action": "add_fsub"}
                txt = (
                    "📢 • <b>𝗔𝗗𝗗 𝗙𝗢𝗥𝗖𝗘-𝗦𝗨𝗕 𝗖𝗛𝗔𝗡𝗡𝗘𝗟</b>\n\n"
                    "<blockquote>Send the channel username or ID now:\n"
                    "Example 1: <code>@TheDrunkBots</code>\n"
                    "Example 2: <code>-1001234567890 https://t.me/+inviteLink My Channel</code>\n\n"
                    "Make sure the bot is an Admin in that channel!</blockquote>"
                )
                kb = {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "adm:fsub_menu"}]]}
                if edit_target:
                    await self.edit_message(chat_id, edit_target, txt, reply_markup=kb)
                else:
                    await self.send_message(chat_id, txt, reply_markup=kb)
                return

            if data.startswith("adm:del_fsub:"):
                cid = data.split("adm:del_fsub:", 1)[1]
                channels = [
                    c for c in (self.db.settings.get("force_sub_channels") or [])
                    if str(c.get("chat_id", "")).lower() != cid.lower()
                ]
                await self.db.update_settings({"force_sub_channels": channels})
                await self._cmd_listfsub(chat_id, None, edit_id=edit_target)
                return

            if data == "adm:cache_menu":
                items = await self.db.list_cache(limit=6)
                lines = [
                    f"• <code>{esc(i['source'])}</code>\n  ➡️ <code>{esc(i['url'][:65])}</code> (hits: {i.get('hits', 0)})"
                    for i in items
                ]
                body = "\n".join(lines) if lines else "Cache is currently empty."
                txt = (
                    f"🗄 • <b>𝗖𝗟𝗢𝗨𝗗 𝗖𝗔𝗖𝗛𝗘 𝗠𝗔𝗡𝗔𝗚𝗘𝗥 ({len(self.db.local_cache)} items)</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"<blockquote>{body}</blockquote>"
                )
                kb = {
                    "inline_keyboard": [
                        [
                            {"text": "➕ Add / Edit Cache Link", "callback_data": "adm:add_cache_prompt"},
                            {"text": "🗑 Delete Cache Link", "callback_data": "adm:del_cache_prompt"},
                        ],
                        [{"text": "🔥 Clear Entire Cache", "callback_data": "adm:clear_cache_confirm"}],
                        [{"text": "🔙 Back to Admin Panel", "callback_data": "adm:home"}],
                    ]
                }
                if edit_target:
                    await self.edit_message(chat_id, edit_target, txt, reply_markup=kb)
                else:
                    await self.send_message(chat_id, txt, reply_markup=kb)
                return

            if data == "adm:add_cache_prompt":
                self._admin_states[user_id] = {"action": "add_cache"}
                txt = (
                    "➕ • <b>𝗔𝗗𝗗 / 𝗠𝗢𝗗𝗜𝗙𝗬 𝗖𝗔𝗖𝗛𝗘 𝗘𝗡𝗧𝗥𝗬</b>\n\n"
                    "<blockquote>Send the shortlink and destination URL separated by <code>|</code>:\n"
                    "<code>https://lksfy.com/abc | https://t.me/dest</code></blockquote>"
                )
                kb = {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "adm:cache_menu"}]]}
                if edit_target:
                    await self.edit_message(chat_id, edit_target, txt, reply_markup=kb)
                else:
                    await self.send_message(chat_id, txt, reply_markup=kb)
                return

            if data == "adm:del_cache_prompt":
                self._admin_states[user_id] = {"action": "del_cache"}
                txt = "🗑 • <b>𝗗𝗘𝗟𝗘𝗧𝗘 𝗖𝗔𝗖𝗛𝗘 𝗘𝗡𝗧𝗥𝗬</b>\n\n<blockquote>Send the shortlink URL you want to delete from cache:</blockquote>"
                kb = {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "adm:cache_menu"}]]}
                if edit_target:
                    await self.edit_message(chat_id, edit_target, txt, reply_markup=kb)
                else:
                    await self.send_message(chat_id, txt, reply_markup=kb)
                return

            if data == "adm:clear_cache_confirm":
                await self.db.clear_cache()
                await self._send_admin_panel(chat_id, edit_id=edit_target)
                return

            if data == "adm:logs_menu":
                await self._cmd_logs(chat_id, edit_id=edit_target)
                return

            if data == "adm:clear_logs":
                await self.db.clear_error_logs()
                await self._cmd_logs(chat_id, edit_id=edit_target)
                return

            if data == "adm:users_menu":
                users = await self.db.list_users(limit=8)
                lines = []
                for u in users:
                    st_lbl = "🚫 BANNED" if u.get("banned") else f"✅ {u.get('bypass_count', 0)} links"
                    lines.append(
                        f"• <code>{u['user_id']}</code> — {esc(u.get('first_name'))} "
                        f"(@{esc(u.get('username') or 'none')}) [{st_lbl}]"
                    )
                body = "\n".join(lines) if lines else "No users recorded yet."
                txt = (
                    f"👥 • <b>𝗨𝗦𝗘𝗥𝗦 &amp; 𝗕𝗥𝗢𝗔𝗗𝗖𝗔𝗦𝗧 ({len(self.db.local_users)} total)</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"<blockquote>{body}</blockquote>"
                )
                kb = {
                    "inline_keyboard": [
                        [{"text": "📢 Broadcast Message to All Users", "callback_data": "adm:broadcast_prompt"}],
                        [
                            {"text": "🚫 Ban User ID", "callback_data": "adm:ban_prompt"},
                            {"text": "✅ Unban User ID", "callback_data": "adm:unban_prompt"},
                        ],
                        [{"text": "🔙 Back to Admin Panel", "callback_data": "adm:home"}],
                    ]
                }
                if edit_target:
                    await self.edit_message(chat_id, edit_target, txt, reply_markup=kb)
                else:
                    await self.send_message(chat_id, txt, reply_markup=kb)
                return

            if data == "adm:broadcast_prompt":
                self._admin_states[user_id] = {"action": "broadcast"}
                txt = "📢 • <b>𝗕𝗥𝗢𝗔𝗗𝗖𝗔𝗦𝗧 𝗠𝗘𝗦𝗦𝗔𝗚𝗘</b>\n\n<blockquote>Send the message text (HTML supported) you want to broadcast to all users:</blockquote>"
                kb = {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "adm:users_menu"}]]}
                if edit_target:
                    await self.edit_message(chat_id, edit_target, txt, reply_markup=kb)
                else:
                    await self.send_message(chat_id, txt, reply_markup=kb)
                return

            if data == "adm:ban_prompt":
                self._admin_states[user_id] = {"action": "ban_user"}
                txt = "🚫 • <b>𝗕𝗔𝗡 𝗨𝗦𝗘𝗥</b>\n\n<blockquote>Send the numeric Telegram User ID to ban:</blockquote>"
                kb = {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "adm:users_menu"}]]}
                if edit_target:
                    await self.edit_message(chat_id, edit_target, txt, reply_markup=kb)
                else:
                    await self.send_message(chat_id, txt, reply_markup=kb)
                return

            if data == "adm:unban_prompt":
                self._admin_states[user_id] = {"action": "unban_user"}
                txt = "✅ • <b>𝗨𝗡𝗕𝗔𝗡 𝗨𝗦𝗘𝗥</b>\n\n<blockquote>Send the numeric Telegram User ID to unban:</blockquote>"
                kb = {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "adm:users_menu"}]]}
                if edit_target:
                    await self.edit_message(chat_id, edit_target, txt, reply_markup=kb)
                else:
                    await self.send_message(chat_id, txt, reply_markup=kb)
                return

            if data == "adm:settings_menu":
                s = self.db.settings
                maint = "🟡 ON" if s.get("maintenance_mode") else "🟢 OFF"
                prot = "🔒 ON" if s.get("protect_content") else "🔓 OFF"
                txt = (
                    f"⚙️ • <b>𝗕𝗢𝗧 &amp; 𝗘𝗡𝗚𝗜𝗡𝗘 𝗦𝗘𝗧𝗧𝗜𝗡𝗚𝗦</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"<blockquote>• <b>Brand :</b> <code>{esc(s.get('brand_name'))}</code>\n"
                    f"• <b>Maintenance Mode :</b> <code>{maint}</code>\n"
                    f"• <b>Protect Content :</b> <code>{prot}</code>\n"
                    f"• <b>Cache TTL :</b> <code>{s.get('cache_hours', 24)} hours</code>\n"
                    f"• <b>Engine Timeout :</b> <code>{s.get('timeout', 300)}s</code>\n"
                    f"• <b>Start Photo :</b> <code>{esc((s.get('start_photo') or '')[:45])}</code>\n"
                    f"• <b>Admins :</b> <code>{esc(s.get('admin_ids'))}</code></blockquote>"
                )
                kb = {
                    "inline_keyboard": [
                        [
                            {"text": f"🛠 Maintenance: {maint}", "callback_data": "adm:toggle_maint"},
                            {"text": f"🔒 Protect: {prot}", "callback_data": "adm:toggle_prot"},
                        ],
                        [
                            {"text": "🖼 Edit Start Photo", "callback_data": "adm:set_photo_prompt"},
                            {"text": "💬 Edit Welcome Text", "callback_data": "adm:set_welcome_prompt"},
                        ],
                        [
                            {"text": "⏱ Edit Cache Hours", "callback_data": "adm:set_cache_hours_prompt"},
                        ],
                        [{"text": "🔙 Back to Admin Panel", "callback_data": "adm:home"}],
                    ]
                }
                if edit_target:
                    await self.edit_message(chat_id, edit_target, txt, reply_markup=kb)
                else:
                    await self.send_message(chat_id, txt, reply_markup=kb)
                return

            if data == "adm:toggle_maint":
                cur = bool(self.db.settings.get("maintenance_mode", False))
                await self.db.update_settings({"maintenance_mode": not cur})
                cb["data"] = "adm:settings_menu"
                await self._on_callback(cb)
                return

            if data == "adm:toggle_prot":
                cur = bool(self.db.settings.get("protect_content", False))
                await self.db.update_settings({"protect_content": not cur})
                cb["data"] = "adm:settings_menu"
                await self._on_callback(cb)
                return

            if data == "adm:set_photo_prompt":
                self._admin_states[user_id] = {"action": "set_photo"}
                txt = "🖼 • <b>𝗦𝗘𝗧 𝗦𝗧𝗔𝗥𝗧 𝗣𝗛𝗢𝗧𝗢 𝗨𝗥𝗟</b>\n\n<blockquote>Send the direct image URL for /start:</blockquote>"
                kb = {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "adm:settings_menu"}]]}
                if edit_target:
                    await self.edit_message(chat_id, edit_target, txt, reply_markup=kb)
                else:
                    await self.send_message(chat_id, txt, reply_markup=kb)
                return

            if data == "adm:set_welcome_prompt":
                self._admin_states[user_id] = {"action": "set_welcome"}
                txt = (
                    "💬 • <b>𝗦𝗘𝗧 𝗪𝗘𝗟𝗖𝗢𝗠𝗘 𝗠𝗘𝗦𝗦𝗔𝗚𝗘</b>\n\n"
                    "<blockquote>Send the new welcome message (HTML allowed; use <code>{first_name}</code> and <code>{brand}</code> placeholders):</blockquote>"
                )
                kb = {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "adm:settings_menu"}]]}
                if edit_target:
                    await self.edit_message(chat_id, edit_target, txt, reply_markup=kb)
                else:
                    await self.send_message(chat_id, txt, reply_markup=kb)
                return

            if data == "adm:set_cache_hours_prompt":
                self._admin_states[user_id] = {"action": "set_cache_hours"}
                txt = "⏱ • <b>𝗦𝗘𝗧 𝗖𝗔𝗖𝗛𝗘 𝗧𝗧𝗟 (𝗛𝗢𝗨𝗥𝗦)</b>\n\n<blockquote>Send the number of hours to keep resolved links in cache (e.g. <code>24</code>):</blockquote>"
                kb = {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": "adm:settings_menu"}]]}
                if edit_target:
                    await self.edit_message(chat_id, edit_target, txt, reply_markup=kb)
                else:
                    await self.send_message(chat_id, txt, reply_markup=kb)
                return
