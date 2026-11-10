#!/usr/bin/env python3
"""Exercise attach and exec response negotiation against a running engine.

Usage: python3 tests/attach-http.py /path/to/docker.sock [image]
The image must already be pulled. Only this script's containers are removed.
"""
import http.client
import json
import socket
import struct
import sys


class Connection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=10)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def request(path, method, url, body=None, expected=200):
    conn = Connection(path)
    try:
        conn.request(method, "/v1.47" + url,
                     None if body is None else json.dumps(body),
                     {"Content-Type": "application/json"})
        response = conn.getresponse()
        data = response.read()
        assert response.status == expected, (url, response.status, data)
        return json.loads(data) if data else None
    finally:
        conn.close()


def output(data, tty):
    if tty:
        return data
    result = bytearray()
    while data:
        assert len(data) >= 8, data
        stream, length = data[0], struct.unpack(">I", data[4:8])[0]
        assert stream in (1, 2) and data[1:4] == b"\0\0\0", data[:8]
        assert len(data) >= 8 + length, data
        result.extend(data[8:8 + length])
        data = data[8 + length:]
    return bytes(result)


def check(path, image, headers, status, tty, attach, stdin):
    command = "read -r value; printf '%s' \"$value\"; printf err >&2" if stdin else "printf out; printf err >&2"
    if stdin and tty:
        command = "stty -echo; printf ready; " + command
    create = {"Image": image, "Cmd": ["sh", "-c", command]
              if attach else ["sleep", "120"], "Tty": tty,
              "OpenStdin": stdin, "HostConfig": {"NetworkMode": "none"}}
    cid = request(path, "POST", "/containers/create", create, 201)["Id"]
    conn = Connection(path)
    writer = None
    try:
        if attach:
            url = f"/containers/{cid}/attach?stream=1&stdout=1&stderr=1&stdin={int(stdin)}"
            body = None
        else:
            request(path, "POST", f"/containers/{cid}/start", expected=204)
            eid = request(path, "POST", f"/containers/{cid}/exec", {
                "Cmd": ["sh", "-c", command], "AttachStdin": stdin,
                "AttachStdout": True, "AttachStderr": True, "Tty": tty,
            }, 201)["Id"]
            url, body = f"/exec/{eid}/start", json.dumps({"Detach": False, "Tty": tty})
        conn.request("POST", "/v1.47" + url, body,
                     {"Content-Type": "application/json", **headers})
        if stdin:
            writer = conn.sock.dup()
        response = conn.getresponse()
        assert response.status == status, (url, headers, response.status)
        assert response.getheader("Content-Length") is None
        assert response.getheader("Transfer-Encoding") is None
        if status == 200:
            assert response.getheader("Content-Type") == "application/vnd.docker.raw-stream"
            assert response.getheader("Upgrade") is None
        else:
            assert response.getheader("Upgrade") == "tcp"
        if attach:
            # SDKs receive a final response before starting the container.
            request(path, "POST", f"/containers/{cid}/start", expected=204)
        if stdin:
            if tty:
                assert response.fp.read(5) == b"ready", "Wait until the terminal has echo disabled"
            writer.sendall(b"out\n")
        # Read the hijacked 101 stream as well as the close-delimited 200 body.
        data = output(response.fp.read(), tty)
        assert data in (b"outerr", b"errout"), data
    finally:
        if writer is not None:
            writer.close()
        conn.close()
        failure = sys.exc_info()[1]
        try:
            request(path, "DELETE", f"/containers/{cid}?force=1", expected=204)
        except Exception as cleanup:
            if failure is None:
                raise
            print(f"Cleanup of {cid} also failed: {cleanup}", file=sys.stderr)


def main():
    path, image = sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "alpine"
    cases = [
        ({}, 200),
        ({"Connection": "Upgrade"}, 200),
        ({"Upgrade": "tcp"}, 101),
        ({"Upgrade": ""}, 101),
        ({"upgrade": "other"}, 101),
        ({"Upgrade": "TCP", "Connection": "keep-alive, Upgrade"}, 101),
    ]
    for attach in (True, False):
        for tty in (False, True):
            for stdin in (False, True):
                for headers, status in cases:
                    check(path, image, headers, status, tty, attach, stdin)
    print("48 attach/exec HTTP negotiation cases passed")


if __name__ == "__main__":
    main()
