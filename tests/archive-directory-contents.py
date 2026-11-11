#!/usr/bin/env python3
"""Check Docker's directory-versus-contents archive naming, including path stat."""
import base64
import contextlib
import http.client
import io
import json
import os
import socket
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
from pathlib import Path

DIRECTORY = "archive-directory-contents"
# Longer than a tar header's 100-byte name and link fields.
LONG_NAME = "n" * 120
LONG_TARGET = "t" * 130
ENTRIES = ["file", "link", "longlink", LONG_NAME, "sub"]


def check(path, image):
    cli = ["docker", "-H", "unix://" + path]

    def run(*args):
        return subprocess.run(cli + list(args), check=True, capture_output=True, timeout=30).stdout

    @contextlib.contextmanager
    def archive(method, source):
        connection = http.client.HTTPConnection("localhost", timeout=5)
        connection.sock = socket.socket(socket.AF_UNIX)
        connection.sock.settimeout(5)
        connection.sock.connect(path)
        try:
            connection.request(method, "/v1.47/containers/" + cid + "/archive?path="
                               + urllib.parse.quote(source, safe=""))
            response = connection.getresponse()
            assert response.status == 200, (method, source, response.status, response.read())
            yield json.loads(base64.b64decode(response.getheader("X-Docker-Container-Path-Stat"))), response
        finally:
            connection.close()

    cid = run("run", "-d", "--network", "none", image, "sleep", "120").decode().strip()
    try:
        run("exec", cid, "sh", "-c",
            "cd /tmp && mkdir -p {0}/sub && printf content > {0}/file && printf long > {0}/{1}"
            " && ln -s /etc {0}/link && ln -s {2} {0}/longlink".format(DIRECTORY, LONG_NAME, LONG_TARGET))
        for state in ("running", "stopped"):
            if state == "stopped":
                run("stop", "-t", "0", cid)
                run("wait", cid)
            # Docker resolves ".." by name before it archives a path, so the
            # parent of "link" is this directory, not the root "link" points
            # into. A stat reports the last component as it was given.
            for suffix, name in (("", DIRECTORY), ("/", DIRECTORY), ("/.", "."), ("/./", "."),
                                 ("/sub/..", DIRECTORY), ("/sub/../", DIRECTORY), ("/sub/../.", "."),
                                 ("/link/..", DIRECTORY)):
                source = "/tmp/" + DIRECTORY + suffix
                with archive("HEAD", source) as (stat, _):
                    assert stat["name"] == source.rstrip("/").rsplit("/", 1)[1], (state, source, stat)
                with archive("GET", source) as (stat, response):
                    assert stat["name"] == name, (state, source, stat)
                    with tarfile.open(fileobj=io.BytesIO(response.read())) as members:
                        expected = [name] + [name + "/" + entry for entry in ENTRIES]
                        assert members.getnames() == expected, (state, source, members.getnames())
                        assert members.extractfile(name + "/file").read() == b"content"
                        assert members.extractfile(name + "/" + LONG_NAME).read() == b"long"
                        assert members.getmember(name + "/longlink").linkname == LONG_TARGET
                print("PASS", state, source, flush=True)
            # The root's entries start with "/": Docker's client puts the new
            # name in front of them. Only the first few are read.
            for source in ("/", "/tmp/.."):
                with archive("GET", source) as (stat, response):
                    assert stat["name"] == "/", (state, source, stat)
                    with tarfile.open(fileobj=response, mode="r|") as members:
                        names = [member.name for member, _ in zip(members, range(3))]
                    # Python reads the root directory's own "/" as "".
                    assert names[0] == "" and len(names) == 3, (state, source, names)
                    assert all(entry.startswith("/") for entry in names[1:]), (state, source, names)
                print("PASS", state, source, flush=True)
            # Docker rebases "." to this new destination name. Every child
            # must keep "./" for that rebase to include it in the directory.
            with tempfile.TemporaryDirectory(prefix="kiri-archive-contents-") as destination:
                copied = Path(destination) / "new-directory"
                run("cp", cid + ":/tmp/" + DIRECTORY + "/.", str(copied))
                assert (copied / "file").read_bytes() == b"content"
                assert (copied / LONG_NAME).read_bytes() == b"long"
                assert os.readlink(copied / "longlink") == LONG_TARGET
                assert not (Path(destination) / "file").exists()
                print("PASS", state, "docker cp into new directory", flush=True)
                # The client rebases the directory's own name here.
                parent = Path(destination) / "parent-directory"
                run("cp", cid + ":/tmp/" + DIRECTORY + "/sub/..", str(parent))
                assert (parent / "file").read_bytes() == b"content"
                assert sorted(os.listdir(destination)) == ["new-directory", "parent-directory"]
                print("PASS", state, "docker cp of a parent into new directory", flush=True)
    finally:
        result = subprocess.run(cli + ["rm", "-f", cid], capture_output=True, timeout=30)
        if result.returncode:
            if sys.exc_info()[0] is None:
                result.check_returncode()
            else:
                print("Cleanup failed:", result.stderr.decode(errors="replace"), file=sys.stderr)


if __name__ == "__main__":
    check(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "alpine")
