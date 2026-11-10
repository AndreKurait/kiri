#!/usr/bin/env python3
"""Check that a container which starts on an internal network goes out through a routed network it joins later."""
import http.client
import io
import json
import socket
import sys
import tarfile
import time
import uuid


class Connection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=60)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def check(path, image):
    containers = []
    networks = []
    tag = "kdr-" + uuid.uuid4().hex[:8] + "-"

    def request(method, target, body=None, statuses=(200,), raw=False):
        connection = Connection(path)
        try:
            connection.request(method, "/v1.47" + target, None if body is None else json.dumps(body),
                               {"Content-Type": "application/json"})
            response = connection.getresponse()
            data = response.read()
            # Track a create even if the assertion below rejects its status.
            if response.status == 201 and target.startswith("/containers/create"):
                containers.append(json.loads(data)["Id"])
            if response.status == 201 and target == "/networks/create":
                networks.append(json.loads(data)["Id"])
            assert response.status in statuses, (method, target, response.status, data)
            if raw:
                return data
            return json.loads(data) if data else None
        finally:
            connection.close()

    # Docker picks the way out among a container's networks by their names;
    # the names here sort in the order the networks are joined.
    def network(name, **settings):
        request("POST", "/networks/create", {"Name": tag + name, **settings}, (201,))
        return tag + name

    def run(net):
        cid = request("POST", "/containers/create", {"Image": image, "Cmd": ["sleep", "120"],
                                                     "HostConfig": {"NetworkMode": net}}, (201,))["Id"]
        request("POST", "/containers/" + cid + "/start", statuses=(204,))
        return cid

    def restart(cid):
        request("POST", "/containers/" + cid + "/stop?t=0", statuses=(204,))
        # The exit may be recorded a moment after the stop returns.
        request("POST", "/containers/" + cid + "/wait?condition=not-running")
        request("POST", "/containers/" + cid + "/start", statuses=(204,))

    # Docker answers 200 to both; Kiri answers 204 today.
    def join(net, cid):
        request("POST", "/networks/" + net + "/connect", {"Container": cid}, (200, 204))

    def leave(net, cid):
        request("POST", "/networks/" + net + "/disconnect", {"Container": cid}, (200, 204))

    def output(cid, command):
        target = "/tmp/route-" + uuid.uuid4().hex
        eid = request("POST", "/containers/" + cid + "/exec",
                      {"Cmd": ["sh", "-c", "(" + command + ") > " + target + " 2>&1"]}, (201,))["Id"]
        # Docker answers 200; Kiri answers 204 today.
        request("POST", "/exec/" + eid + "/start", {"Detach": True}, (200, 204))
        deadline = time.monotonic() + 20
        while True:
            state = request("GET", "/exec/" + eid + "/json")
            if not state["Running"]:
                break
            assert time.monotonic() < deadline, "route probe exec did not exit"
            time.sleep(.01)
        data = request("GET", "/containers/" + cid + "/archive?path=" + target, raw=True)
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            text = archive.extractfile(archive.getmembers()[0]).read().decode()
        assert state["ExitCode"] == 0, (command, state["ExitCode"], text)
        return text

    # The gateways of the container's default routes, IPv4 and IPv6; an
    # empty string where it has none. More than one in a family is wrong.
    def ways_out(cid):
        found = []
        for family in ("-4", "-6"):
            routes = output(cid, "ip " + family + " route").splitlines()
            via = [line.split()[2] for line in routes if line.startswith("default via ")]
            assert len(via) <= 1, (family, routes)
            found.append(via[0] if via else "")
        return tuple(found)

    # What inspect shows as the gateways of the container's endpoint there.
    def gateways(cid, net):
        endpoint = request("GET", "/containers/" + cid + "/json")["NetworkSettings"]["Networks"][net]
        assert endpoint["Gateway"] and endpoint["IPv6Gateway"], (net, endpoint)
        return endpoint["Gateway"], endpoint["IPv6Gateway"]

    passed = 0
    try:
        closed = network("a-closed", Internal=True)
        first = network("b-first", EnableIPv6=True,
                        IPAM={"Config": [{"Subnet": "172.31.244.0/24"}, {"Subnet": "fd42:7a::/64"}]})
        second = network("c-second", EnableIPv6=True,
                         IPAM={"Config": [{"Subnet": "172.31.245.0/24"}, {"Subnet": "fd42:7b::/64"}]})

        # On the internal network alone there is no way out. The first
        # routed network the container joins gives it one.
        cid = run(closed)
        assert ways_out(cid) == ("", ""), ways_out(cid)
        join(first, cid)
        assert ways_out(cid) == gateways(cid, first), (ways_out(cid), gateways(cid, first))
        assert gateways(cid, first)[0].startswith("172.31.244.") and gateways(cid, first)[1].startswith("fd42:7a::")
        passed += 1
        print("PASS a routed network joined after an internal one is the way out", flush=True)

        # A container that starts with both has it from the start.
        restart(cid)
        assert ways_out(cid) == gateways(cid, first), (ways_out(cid), gateways(cid, first))
        passed += 1
        print("PASS after a restart with both networks", flush=True)

        # One more routed network adds its subnets and no second way out.
        join(second, cid)
        assert ways_out(cid) == gateways(cid, first), (ways_out(cid), gateways(cid, first))
        subnets = output(cid, "ip -4 route")
        assert "172.31.244.0/24" in subnets and "172.31.245.0/24" in subnets, subnets
        passed += 1
        print("PASS a second routed network leaves the way out where it is", flush=True)

        # When the network the container went out through leaves, the one
        # that is left takes over.
        leave(first, cid)
        assert ways_out(cid) == gateways(cid, second), (ways_out(cid), gateways(cid, second))
        passed += 1
        print("PASS the way out moves on when its network leaves", flush=True)

        restart(cid)
        assert ways_out(cid) == gateways(cid, second), (ways_out(cid), gateways(cid, second))
        passed += 1
        print("PASS after a restart with what is left", flush=True)

        # Back on the internal network alone there is none again.
        leave(second, cid)
        assert ways_out(cid) == ("", ""), ways_out(cid)
        passed += 1
        print("PASS no way out once the last routed network leaves", flush=True)

        # A container that starts on a routed network keeps going out
        # through it, whatever joins later.
        cid = run(first)
        before = gateways(cid, first)
        assert ways_out(cid) == before, (ways_out(cid), before)
        join(second, cid)
        join(closed, cid)
        assert ways_out(cid) == before, (ways_out(cid), before)
        passed += 1
        print("PASS a container that starts on a routed network keeps its way out", flush=True)
        print(passed, "late default route scenarios passed")
    finally:
        primary_failure = sys.exc_info()[0] is not None
        errors = []
        for cid in reversed(containers):
            try:
                request("DELETE", "/containers/" + cid + "?force=true", statuses=(204, 404))
            except Exception as error:
                errors.append(error)
                print("Cleanup failed:", repr(error), file=sys.stderr)
        for net in reversed(networks):
            try:
                request("DELETE", "/networks/" + net, statuses=(204, 404))
            except Exception as error:
                errors.append(error)
                print("Cleanup failed:", repr(error), file=sys.stderr)
        if errors and not primary_failure:
            raise RuntimeError("Could not remove everything the late default route test made") from errors[0]


if __name__ == "__main__":
    check(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "alpine")
