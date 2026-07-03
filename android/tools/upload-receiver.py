#!/usr/bin/env python3
"""Dev receiver for HiNote Sync uploads: dumps each POST body to ./received/.
Usage: python3 upload-receiver.py [port]   (default 8080)
Point the app's Server URL at http://<laptop-ip>:<port>/notes
"""
import pathlib
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

OUT = pathlib.Path("received")
OUT.mkdir(exist_ok=True)

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        name = OUT / f"upload-{int(time.time() * 1000)}.multipart"
        name.write_bytes(body)
        print(f"{self.path}: {len(body)} bytes -> {name} "
              f"(auth: {self.headers.get('X-Api-Key', '-')})")
        self.send_response(200)
        self.end_headers()

HTTPServer(("0.0.0.0", int(sys.argv[1]) if len(sys.argv) > 1 else 8080), Handler).serve_forever()
