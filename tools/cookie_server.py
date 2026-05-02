"""Local HTTP server to capture Meta AI cookies from your real browser.

Usage:
    1. Run:   python3 tools/cookie_server.py
    2. In your ALREADY LOGGED-IN browser, visit:
       http://localhost:8765/
    3. The page will auto-extract cookies and save them.
    4. Stop the server with Ctrl+C.
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

COOKIE_FILE = Path(".meta_ai_cookies.json")
DEFAULT_PORT = 8765

_HTML = (  # noqa: E501
    "<!DOCTYPE html><html><head><title>Meta AI Cookie Extractor</title>"
    "<style>"
    "body{font-family:system-ui,sans-serif;max-width:600px;margin:40px auto;padding:20px}"
    "h1{color:#1877f2}"
    ".status{padding:15px;border-radius:8px;margin:20px 0}"
    ".success{background:#e7f3ff;color:#1877f2}"
    ".error{background:#ffe7e7;color:#c00}"
    "pre{background:#f5f5f5;padding:15px;border-radius:8px;overflow-x:auto}"
    "button{background:#1877f2;color:#fff;border:none;padding:12px 24px;"
    "border-radius:6px;font-size:16px;cursor:pointer}"
    "button:hover{background:#166fe5}"
    "</style></head><body>"
    "<h1>Meta AI Cookie Extractor</h1>"
    "<p>Extract cookies from your logged-in Meta AI session.</p>"
    "<button onclick=\"extract()\">Extract Cookies</button>"
    "<div id=\"result\"></div>"
    "<script>"
    "async function extract(){"
    "const r=document.getElementById(\"result\");"
    "r.innerHTML='<div class=\"status\">Extracting...</div>';"
    "const c=document.cookie.split(\"; \").reduce((a,p)=>{"
    "const[n,...v]=p.split(\"=\");if(n)a[n]=v.join(\"=\");return a},{});"
    "let l=\"\";document.querySelectorAll(\"script\").forEach(s=>{"
    "const t=s.textContent||\"\";const m=t.match(/\"LSD\",\\[],\\{\"token\":\"([^\"]+)\"/);"
    "if(m)l=m[1]});if(l)c.lsd=l;"
    "let d=\"\";const i=document.querySelector('input[name=\"fb_dtsg\"]');"
    "if(i)d=i.value;if(d)c.fb_dtsg=d;"
    "const resp=await fetch(\"/save\",{method:\"POST\","
    "headers:{\"Content-Type\":\"application/json\"},"
    "body:JSON.stringify(c)});"
    "const data=await resp.json();"
    "if(data.success){"
    "r.innerHTML='<div class=\"status success\">Cookies saved!</div>';"
    "}else{r.innerHTML='<div class=\"status error\">Error: '+data.error+'</div>';}"
    "}"
    "</script></body></html>"
)


class Handler(BaseHTTPRequestHandler):
    """Handle GET / and POST /save."""

    def log_message(self, fmt: str, *args) -> None:
        """Suppress default access logging noise."""
        pass

    def do_GET(self) -> None:
        """Serve the extraction page."""
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(_HTML.encode())

    def do_POST(self) -> None:
        """Save cookies sent from the browser."""
        if self.path != "/save":
            self._send_json(404, {"error": "Not found"})
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        try:
            cookies = json.loads(body)
        except json.JSONDecodeError:
            self._send_json(400, {"error": "Invalid JSON"})
            return

        has_lsd = "lsd" in cookies
        is_authed = "c_user" in cookies or "abra_sess" in cookies

        payload = {
            "cookies": cookies,
            "is_authenticated": is_authed,
            "created_at": __import__("time").time(),
        }
        COOKIE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        msg = f"Saved {len(cookies)} cookies, LSD={has_lsd}, authed={is_authed}"
        print(msg)
        self._send_json(200, {"success": True, "has_lsd": has_lsd})

    def _send_json(self, status: int, data: dict) -> None:
        """Send a JSON response."""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_OPTIONS(self) -> None:
        """Handle CORS preflight."""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


def main() -> None:
    """Start the local cookie capture server."""
    parser = argparse.ArgumentParser(description="Meta AI Cookie Capture Server")
    parser.add_argument(
        "-p", "--port", type=int, default=DEFAULT_PORT,
        help=f"Port to run the server on (default: {DEFAULT_PORT})"
    )
    args = parser.parse_args()
    port = args.port

    print("=" * 60)
    print("Meta AI Cookie Capture Server")
    print("=" * 60)
    print()
    print(f"Server running at: http://localhost:{port}/")
    print()
    print("INSTRUCTIONS:")
    print("1. Open your ALREADY LOGGED-IN browser")
    print("2. Navigate to: https://www.meta.ai/")
    print(f"3. In a NEW TAB, visit: http://localhost:{port}/")
    print("4. Click 'Extract Cookies'")
    print("5. Stop this server with Ctrl+C")
    print()

    # Allow socket address reuse so the server can restart immediately
    class ReuseAddrHTTPServer(HTTPServer):
        allow_reuse_address = True

    server = ReuseAddrHTTPServer(("", port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")


if __name__ == "__main__":
    main()
