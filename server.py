import http.server
import os
import socketserver
import threading
import time
import webbrowser
from pathlib import Path

PORT = 8765
DIRECTORY = Path(__file__).parent


class CustomHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DIRECTORY), **kwargs)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self.path = "/dashboard.html"
        return super().do_GET()

    def log_message(self, format, *args):
        pass


def open_browser():
    time.sleep(0.8)
    url = f"http://localhost:{PORT}"
    print(f"\n[+] Visual Terminal opened in browser: {url}")
    webbrowser.open(url)


def main():
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", PORT), CustomHandler) as httpd:
        print("=" * 70)
        print("NAUTILUSTRADER // BYBIT v5 OPTIONS MONITOR SERVER ACTIVE")
        print(f"URL: http://localhost:{PORT}")
        print("Press Ctrl+C to terminate.")
        print("=" * 70)

        threading.Thread(target=open_browser, daemon=True).start()

        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down monitor server.")


if __name__ == "__main__":
    main()
