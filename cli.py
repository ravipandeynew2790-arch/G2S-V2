"""Command line: python cli.py https://get2short.com/4mEa [more urls] [--show] [--debug] [--json]"""
import asyncio, json, os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__)); os.chdir(HERE)
from g2s.engine import Bypasser, BypassError, Config

async def main():
    urls = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not urls:
        print(__doc__); return 1
    cfg = Config.load("config.json")
    cfg.show_browser = cfg.show_browser or "--show" in sys.argv
    cfg.debug = cfg.debug or "--debug" in sys.argv
    quiet = "--json" in sys.argv
    b = Bypasser(cfg); out = []; code = 0
    try:
        for u in urls:
            t = time.time()
            log = (lambda m: None) if quiet else (lambda m, t=t: print(f"[{time.time()-t:5.1f}s] {m}", file=sys.stderr, flush=True))
            try:
                r = await b.resolve(u, log=log); out.append(r)
                if not quiet: print(r["url"], flush=True)
            except BypassError as e:
                code = 2; out.append({"source": u, "error": str(e)})
                if not quiet: print(f"ERROR {u}: {e}", file=sys.stderr)
    finally:
        await b.close()
    if quiet: print(json.dumps(out, indent=1))
    return code

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
