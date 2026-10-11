# TheDrunkBots G2S — Deploy & Configuration Guide

Complete shortlink bypasser engine with:
- **Telegram Bot (`TheDrunkBots`)** featuring live 14-segment progress bar & ETA, `COPY LINK 📋` + `OPEN LINK ↗️` buttons, `/start` photo welcome message, Force-Subscribe channels, and an in-bot Admin Panel (`/admin`).
- **Password-Protected Modern Dark Web Admin Panel** (live browser screenshot disabled; full CRUD on Tunnel/Proxy, Cloud Cache, Force-Sub Channels, Error Logs, Bot Users, Broadcast, and Credentials).
- **MongoDB Atlas Real-Time Cloud Sync & Cache** (`g2sv2` database) so repeated links return in `0.01s` and all changes sync instantly between the Web Admin UI and Telegram Bot.
- **Automatic Telegram Alerts to Owner** whenever a bypass errors (with complete step log) or the Pinggy tunnel expires.

---

## 1. Configuration (`config.json` or Environment Variables)

You can set these directly in `config.json` (or via Railway/Docker environment variables, or live from the Web Admin Panel):

| `config.json` Key | Env Variable Override | Description |
|---|---|---|
| `admin_password` | `G2S_ADMIN_PASSWORD` | Password to unlock the Web Admin Panel (default: `admin123`) |
| `telegram_bot_token` | `G2S_BOT_TOKEN` / `BOT_TOKEN` | Telegram Bot Token from `@BotFather` |
| `telegram_api_id` | `G2S_API_ID` / `API_ID` | Telegram API ID from `my.telegram.org` |
| `telegram_api_hash` | `G2S_API_HASH` / `API_HASH` | Telegram API Hash from `my.telegram.org` |
| `owner_id` | `G2S_OWNER_ID` / `OWNER_ID` | Numeric Telegram User ID of the Owner (gets tunnel & error alerts) |
| `admin_ids` | `G2S_ADMIN_IDS` / `ADMIN_IDS` | List (or comma-separated string) of Admin Telegram User IDs |
| `mongo_uri` | `G2S_MONGO_URI` / `MONGO_URI` | MongoDB Atlas connection string |
| `proxy` | `G2S_PROXY` | Pinggy / residential HTTP proxy (`http://user:pass@host:port`) |
| `solver_url` | `G2S_SOLVER_URL` | Turnstile solver URL (`https://cf.vltcx.eu.cc`) |
| `start_photo` | `G2S_START_PHOTO` | `/start` welcome photo (`https://i.ibb.co/1Nx1jMn/IMG-20261005-021725.png`) |

---

## 2. Telegram Bot Commands

### User Commands
- `/start` — Welcome message with photo & **TheDrunkBots** branding
- `/help` — Guide & supported shorteners list
- `/bypass <link>` (or send any link directly in chat) — Bypass link with live progress bar & ETA
- `/status` / `/ping` — Check bot & cloud cache status
- `/myinfo` — View user profile & total bypassed links

### Owner / Admin Commands (Only accessible by `owner_id` / `admin_ids`)
- `/admin` or `/panel` — Open interactive **In-Bot Admin Control Panel**
- `/setproxy <http://user:pass@host:port | off>` — Update tunnel proxy live
- `/testproxy` — Test current exit IP & residential check
- `/fsub` — Open Force-Subscribe channel manager
- `/addfsub <@channel or -100id> [invite_link] [title]` — Add Force-Subscribe channel
- `/delfsub <@channel or -100id>` — Remove Force-Subscribe channel
- `/clearcache` / `/delcache <url>` — Clear or delete cached links
- `/logs` / `/clearlogs` — View or clear recent error logs
- `/broadcast <message>` (or reply to a message with `/broadcast`) — Broadcast to all users
- `/ban <user_id>` / `/unban <user_id>` — Ban or unban a user
- `/addadmin <user_id>` / `/deladmin <user_id>` — Add or remove admin IDs
