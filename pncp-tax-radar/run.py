"""Sobe o PNCP Tax Radar em http://127.0.0.1:8000 e abre o navegador.

Uso: python run.py [--no-browser] [--port 8000]
"""

import argparse
import threading
import webbrowser

import uvicorn

from app.config import load_settings
from app.main import create_app


def main() -> None:
    settings = load_settings()
    parser = argparse.ArgumentParser(description="PNCP Tax Radar")
    parser.add_argument("--no-browser", action="store_true", help="não abrir o navegador")
    parser.add_argument("--port", type=int, default=settings.port)
    parser.add_argument("--host", default=settings.host)
    args = parser.parse_args()
    settings.port, settings.host = args.port, args.host

    url = f"http://{args.host}:{args.port}"
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    print(f"PNCP Tax Radar rodando em {url}  (Ctrl+C para parar)")
    uvicorn.run(create_app(settings), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
