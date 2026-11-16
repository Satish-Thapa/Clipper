"""Run the server: `python -m clipper` (hot reload: restarts by itself when a .py file in clipper/ changes).

    python -m clipper                 http://127.0.0.1:8000, reload on code changes
    python -m clipper --port 8080
    python -m clipper --no-reload     plain server (nothing restarts it)

Only clipper/*.py is watched (not .venv, data/ or the downloads). The UI (static/index.html) is read fresh on every
page load, so a browser refresh is enough for it. .env changes still need a manual restart (Ctrl+C, run again).
A reload restarts the process: a video being clipped at that moment is marked "interrupted" (queued topics resume)."""
import argparse
from pathlib import Path

import uvicorn

if __name__ == "__main__":
    a = argparse.ArgumentParser(prog="python -m clipper")
    a.add_argument("--host", default="127.0.0.1")
    a.add_argument("--port", type=int, default=8000)
    a.add_argument("--no-reload", action="store_true", help="don't restart on code changes")
    args = a.parse_args()
    here = Path(__file__).resolve().parent
    print(f"Clipper on http://{args.host}:{args.port}"
          + ("" if args.no_reload else f"  (hot reload: watching {here}\\*.py)"))
    uvicorn.run("clipper.app:app", host=args.host, port=args.port, reload=not args.no_reload,
                reload_dirs=[str(here)], reload_includes=["*.py"])
