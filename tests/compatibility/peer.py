#!/usr/bin/env python3
"""Disposable external UDP observer, used to verify actual source NAT behavior."""
import http.server
import json
import socket
import threading

observed = {}
lock = threading.Lock()


def receive():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 9000))
    while True:
        data, address = sock.recvfrom(2048)
        with lock:
            observed[data.decode().strip()] = address[0]


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        with lock:
            value = observed.get(self.path.removeprefix("/"))
        data = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):
        pass


threading.Thread(target=receive, daemon=True).start()
http.server.HTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
