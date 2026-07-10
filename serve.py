import os

from waitress import serve

from app import app, background_refresh, init_db


if __name__ == "__main__":
    init_db()
    background_refresh()
    host = os.environ.get("FUND_APP_HOST", "0.0.0.0")
    port = int(os.environ.get("FUND_APP_PORT", "8765"))
    serve(app, host=host, port=port)
