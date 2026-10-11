"""Client for the cv3inx/turnstile-solver ("Violetics Solver") HTTP API."""
import asyncio
import time

import aiohttp


class SolverError(Exception):
    pass


class TurnstileSolver:
    def __init__(self, base_url: str, api_key: str | None = None, timeout: int = 45,
                 min_interval: float = 6.5):
        self.base = base_url.rstrip("/")
        self.api_key = api_key or None
        self.timeout = timeout
        # The solver bans IPs that exceed RATE_LIMIT_PER_MIN (default 10/min),
        # so we space requests out on our side.
        self.min_interval = min_interval
        self._lock = asyncio.Lock()
        self._last = 0.0

    def _headers(self):
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["X-API-Key"] = self.api_key
        return h

    async def health(self) -> dict:
        async with aiohttp.ClientSession() as s:
            async with s.get(self.base + "/health", timeout=aiohttp.ClientTimeout(total=15)) as r:
                return await r.json(content_type=None)

    async def solve_turnstile(self, sitekey: str, siteurl: str, action: str | None = None,
                              cdata: str | None = None) -> str:
        body = {"sitekey": sitekey, "siteurl": siteurl, "timeout": self.timeout}
        if action:
            body["action"] = action
        if cdata:
            body["cdata"] = cdata
        async with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()
        async with aiohttp.ClientSession() as s:
            async with s.post(self.base + "/solve", json=body, headers=self._headers(),
                              timeout=aiohttp.ClientTimeout(total=self.timeout + 25)) as r:
                data = await r.json(content_type=None)
        if "token" not in data:
            raise SolverError(f"{data.get('error_code')}: {data.get('error')}")
        return data["token"]
