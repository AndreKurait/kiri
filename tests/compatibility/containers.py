#!/usr/bin/env python3
"""Container/network behavior checks and lifecycle benchmarks over the Engine API.

Use only against a disposable test engine. Objects have unique names and are
removed in finally blocks; pulling the test image populates the engine's cache.
No Python dependencies beyond the standard library.
"""
import argparse
import base64
import concurrent.futures
import http.client
import ipaddress
import json
import math
import os
import socket
import statistics
import struct
import time
import uuid
from urllib.parse import quote


class Connection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=60)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class Engine:
    def __init__(self, path, image):
        self.path, self.image = path, image
        self.prefix = "compat-" + uuid.uuid4().hex[:12]
        self.containers, self.networks = [], []
        self.results = []

    def request(self, method, path, body=None, status=200, raw=False):
        conn = Connection(self.path)
        try:
            headers = {"Content-Type": "application/json"}
            conn.request(method, "/v1.48" + path, None if body is None else json.dumps(body), headers)
            resp = conn.getresponse()
            data = resp.read()
            assert resp.status == status, (method, path, body, resp.status, data[:600].decode(errors="replace"))
            if raw or not data:
                return data
            return json.loads(data)
        finally:
            conn.close()

    def create(self, cmd=None, host=None, endpoints=None, **config):
        body = dict(Image=self.image, Cmd=cmd or ["sleep", "120"], HostConfig=host or {},
                    Labels={"fork.compatibility": self.prefix}, **config)
        if endpoints:
            body["NetworkingConfig"] = {"EndpointsConfig": endpoints}
        value = self.request("POST", "/containers/create", body, 201)
        self.containers.append(value["Id"])
        return value["Id"]

    def inspect(self, cid):
        return self.request("GET", f"/containers/{cid}/json")

    def run(self, cmd, host=None, endpoints=None):
        cid = self.create(cmd, host, endpoints)
        self.request("POST", f"/containers/{cid}/start", status=204)
        result = self.request("POST", f"/containers/{cid}/wait")
        assert result["StatusCode"] == 0, result
        output = self.request("GET", f"/containers/{cid}/logs?stdout=1&stderr=1", raw=True)
        return cid, output

    def network(self, suffix, **settings):
        value = self.request("POST", "/networks/create",
                             dict(Name=self.prefix + suffix, Labels={"fork.compatibility": self.prefix}, **settings), 201)
        self.networks.append(value["Id"])
        return value["Id"]

    def test(self, name, fn):
        start = time.monotonic()
        try:
            fn()
            self.results.append(dict(name=name, passed=True, seconds=time.monotonic() - start))
            print("PASS", name, flush=True)
        except Exception as exc:
            self.results.append(dict(name=name, passed=False, error=str(exc), seconds=time.monotonic() - start))
            print("FAIL", name, str(exc), flush=True)

    def cleanup(self):
        for cid in reversed(self.containers):
            try:
                self.request("DELETE", f"/containers/{cid}?force=1&v=1", status=204)
            except Exception as exc:
                self.results.append(dict(name="cleanup-container", passed=False, error=str(exc)))
        for nid in reversed(self.networks):
            try:
                self.request("DELETE", f"/networks/{nid}", status=204)
            except Exception as exc:
                self.results.append(dict(name="cleanup-network", passed=False, error=str(exc)))

    def lifecycle(self):
        cid, output = self.run(["sh", "-c", "printf stdout; printf stderr >&2"])
        assert b"stdout" in output and b"stderr" in output
        assert self.inspect(cid)["State"]["Status"] == "exited"
        name = self.prefix + "-renamed"
        self.request("POST", f"/containers/{cid}/rename?name={name}", status=204)
        assert self.inspect(cid)["Name"] == "/" + name
        self.request("POST", f"/containers/{cid}/start", status=204)
        assert self.request("POST", f"/containers/{cid}/wait")["StatusCode"] == 0

    def pause_restart_exec(self):
        cid = self.create()
        self.request("POST", f"/containers/{cid}/start", status=204)
        self.request("POST", f"/containers/{cid}/start", status=304)
        self.request("POST", f"/containers/{cid}/pause", status=204)
        assert self.inspect(cid)["State"]["Paused"]
        self.request("POST", f"/containers/{cid}/unpause", status=204)
        assert not self.inspect(cid)["State"]["Paused"]
        eid = self.request("POST", f"/containers/{cid}/exec", {"Cmd": ["true"]}, 201)["Id"]
        # Docker documents 200 for detached exec start (not 204).
        self.request("POST", f"/exec/{eid}/start", {"Detach": True}, 200)
        for _ in range(100):
            state = self.request("GET", f"/exec/{eid}/json")
            if not state["Running"]:
                break
            time.sleep(.02)
        assert not state["Running"] and state["ExitCode"] == 0, state
        self.request("POST", f"/containers/{cid}/restart?t=0", status=204)
        assert self.inspect(cid)["State"]["Running"]
        self.request("POST", f"/containers/{cid}/stop?t=0", status=204)
        assert not self.inspect(cid)["State"]["Running"]
        self.request("POST", f"/containers/{cid}/stop?t=0", status=304)

    def resource_reset(self):
        cid = self.create(host={"PidsLimit": 32, "CpuQuota": 20000, "CpuPeriod": 100000})
        self.request("POST", f"/containers/{cid}/update", {"PidsLimit": -1, "CpuQuota": -1})
        # Verify enforcement after update, not just echoed inspect fields.
        self.request("POST", f"/containers/{cid}/start", status=204)
        eid = self.request("POST", f"/containers/{cid}/exec", {
            "Cmd": ["sh", "-c", "cat /sys/fs/cgroup/pids.max /sys/fs/cgroup/cpu.max > /limits"],
        }, 201)["Id"]
        self.request("POST", f"/exec/{eid}/start", {"Detach": True}, 200)
        for _ in range(100):
            state = self.request("GET", f"/exec/{eid}/json")
            if not state["Running"]:
                break
            time.sleep(.02)
        assert not state["Running"] and state["ExitCode"] == 0
        import io
        import tarfile
        archive = self.request("GET", f"/containers/{cid}/archive?path=/limits", raw=True)
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            values = tar.extractfile(tar.getmembers()[0]).read().decode().splitlines()
        assert values == ["max", "max 100000"], values

    def invalid_updates(self):
        cid = self.create()
        for value in [
            {"RestartPolicy": {"Name": "invalid"}},
            {"RestartPolicy": {"Name": "always", "MaximumRetryCount": 2}},
            {"RestartPolicy": {"Name": "on-failure", "MaximumRetryCount": -1}},
            {"CpuPeriod": 1}, {"Memory": -1},
            {"NanoCpus": 1000000000, "CpuQuota": 10000},
        ]:
            error = self.request("POST", f"/containers/{cid}/update", value, 400)
            assert error.get("message"), error
        assert self.inspect(cid)["HostConfig"]["RestartPolicy"]["Name"] == "no"

    def websocket_attach(self, stdin_once=False, version=48):
        cid = self.create(["cat"], OpenStdin=True, StdinOnce=stdin_once)
        self.request("POST", f"/containers/{cid}/start", status=204)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(10)
        try:
            sock.connect(self.path)
            key = base64.b64encode(os.urandom(16)).decode()
            path = f"/v1.{version}/containers/{cid}/attach/ws?stream=1"
            if version >= 42:
                path += "&stdin=1&stdout=1&stderr=1"
            sock.sendall((f"GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: Upgrade\r\n"
                          f"Upgrade: websocket\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: {key}\r\n\r\n").encode())
            with sock.makefile("rb") as response:
                status = response.readline()
                assert b" 101 " in status, status
                while True:
                    line = response.readline()
                    assert line, "WebSocket upgrade headers ended prematurely"
                    if line == b"\r\n":
                        break
                data, mask = b"websocket-roundtrip\n", os.urandom(4)
                sock.sendall(bytes([0x82, 0x80 | len(data)]) + mask +
                             bytes(value ^ mask[i % 4] for i, value in enumerate(data)))
                output = b""
                while len(output) < len(data):
                    first, length = response.read(2)
                    assert first & 0xf == (2 if version >= 28 else 1), first
                    assert not length & 0x80
                    if length == 126:
                        length = struct.unpack("!H", response.read(2))[0]
                    elif length == 127:
                        length = struct.unpack("!Q", response.read(8))[0]
                    output += response.read(length)
                assert output == data, output
                sock.sendall(b"\x88\x82" + mask + bytes(v ^ mask[i] for i, v in enumerate(b"\x03\xe8")))
        finally:
            sock.close()
        if stdin_once:
            for _ in range(100):
                if not self.inspect(cid)["State"]["Running"]:
                    break
                time.sleep(.02)
            assert not self.inspect(cid)["State"]["Running"], "StdinOnce must close stdin on disconnect"
        else:
            time.sleep(.1)
            assert self.inspect(cid)["State"]["Running"], "Disconnect must preserve stdin when StdinOnce is false"

    def addressing(self, ipv4, ipv6):
        config = []
        if ipv4:
            config.append({"Subnet": "10.231.17.0/24", "Gateway": "10.231.17.1"})
        if ipv6:
            config.append({"Subnet": "fd42:cafe:17::/64", "Gateway": "fd42:cafe:17::1"})
        nid = self.network("-addr-" + str(len(self.networks)), EnableIPv4=ipv4, EnableIPv6=ipv6,
                           IPAM={"Config": config})
        details = self.request("GET", f"/networks/{nid}")
        name = details["Name"]
        addr = {}
        if ipv4:
            addr["IPv4Address"] = "10.231.17.42"
        if ipv6:
            addr["IPv6Address"] = "fd42:cafe:17::42"
        cid, output = self.run(["sh", "-c", "ip -4 addr show eth0; ip -6 addr show eth0"],
                               {"NetworkMode": name}, {name: {"IPAMConfig": addr}})
        state = self.inspect(cid)["NetworkSettings"]["Networks"][name]
        # Addresses disappear on Docker after exit; inspect a running peer.
        live = self.create(host={"NetworkMode": name}, endpoints={name: {"IPAMConfig": addr}})
        self.request("POST", f"/containers/{live}/start", status=204)
        state = self.inspect(live)["NetworkSettings"]["Networks"][name]
        assert bool(state["IPAddress"]) == ipv4, state
        assert bool(state["GlobalIPv6Address"]) == ipv6, state
        if ipv4:
            assert ipaddress.ip_address(state["IPAddress"]) == ipaddress.ip_address(addr["IPv4Address"])
            assert b"10.231.17.42/24" in output
        if ipv6:
            assert ipaddress.ip_address(state["GlobalIPv6Address"]) == ipaddress.ip_address(addr["IPv6Address"])
            assert b"fd42:cafe:17::42/64" in output
        # Remove stopped and live endpoints so subsequent address-family tests
        # can reuse the same subnets without relying on engine-specific pruning.
        for obj in (cid, live):
            self.request("DELETE", f"/containers/{obj}?force=1", status=204)
            self.containers.remove(obj)
        self.request("DELETE", f"/networks/{nid}", status=204)
        self.networks.remove(nid)

    def connect_disconnect(self):
        n1 = self.network("-one", EnableIPv6=True)
        n2 = self.network("-two", EnableIPv6=True)
        name1 = self.request("GET", f"/networks/{n1}")["Name"]
        name2 = self.request("GET", f"/networks/{n2}")["Name"]
        cid = self.create(host={"NetworkMode": name1})
        self.request("POST", f"/containers/{cid}/start", status=204)
        self.request("POST", f"/networks/{n2}/connect", {"Container": cid, "EndpointConfig": {"Aliases": ["peer"]}})
        assert name2 in self.inspect(cid)["NetworkSettings"]["Networks"]
        _, output = self.run(["ping", "-6", "-c", "1", "-W", "3", "peer"], {"NetworkMode": name2})
        assert b"1 packets received" in output
        self.request("POST", f"/networks/{n2}/disconnect", {"Container": cid})
        assert name2 not in self.inspect(cid)["NetworkSettings"]["Networks"]

    def exec_capture(self, cid, command):
        import io
        import tarfile
        path = "/tmp/compat-" + uuid.uuid4().hex
        eid = self.request("POST", f"/containers/{cid}/exec", {
            "Cmd": ["sh", "-c", "(" + command + ") > " + path + " 2>&1"],
        }, 201)["Id"]
        self.request("POST", f"/exec/{eid}/start", {"Detach": True})
        for _ in range(500):
            state = self.request("GET", f"/exec/{eid}/json")
            if not state["Running"]:
                break
            time.sleep(.02)
        assert not state["Running"], "exec did not exit"
        data = self.request("GET", f"/containers/{cid}/archive?path=" + path, raw=True)
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            output = tar.extractfile(tar.getmembers()[0]).read()
        return state["ExitCode"], output

    def advanced_resources(self):
        cpu = min(os.sched_getaffinity(0))
        mems = open("/sys/fs/cgroup/cpuset.mems.effective").read().strip().split(",")[0].split("-")[0]
        cid = self.create(host={"NetworkMode": "none", "Memory": 67108864, "MemorySwap": 100663296,
                                "MemoryReservation": 16777216, "CpusetCpus": str(cpu), "CpusetMems": mems})
        self.request("POST", f"/containers/{cid}/start", status=204)
        command = "cat /sys/fs/cgroup/memory.max /sys/fs/cgroup/memory.swap.max /sys/fs/cgroup/memory.low /sys/fs/cgroup/cpuset.cpus /sys/fs/cgroup/cpuset.mems"
        code, output = self.exec_capture(cid, command)
        assert code == 0 and output.decode().splitlines() == ["67108864", "33554432", "16777216", str(cpu), mems], (code, output)
        self.request("POST", f"/containers/{cid}/update", {"Memory": 67108864, "MemorySwap": 134217728, "MemoryReservation": 8388608})
        code, output = self.exec_capture(cid, command)
        assert code == 0 and output.decode().splitlines()[:3] == ["67108864", "67108864", "8388608"], (code, output)
        unlimited = self.create(host={"NetworkMode": "none", "Memory": 67108864, "MemorySwap": -1})
        self.request("POST", f"/containers/{unlimited}/start", status=204)
        assert self.exec_capture(unlimited, "cat /sys/fs/cgroup/memory.swap.max") == (0, b"max\n")
        old = self.inspect(cid)["HostConfig"]
        self.request("POST", f"/containers/{cid}/update", {"Memory": 67108864, "MemorySwap": 1}, 400)
        assert self.inspect(cid)["HostConfig"]["MemorySwap"] == old["MemorySwap"]

    def endpoint_metadata(self):
        nid = self.network("-metadata", EnableIPv6=True)
        network = self.request("GET", f"/networks/{nid}")
        name = network["Name"]
        cid = self.create(host={"NetworkMode": name})
        self.request("POST", f"/containers/{cid}/start", status=204)
        endpoint = self.inspect(cid)["NetworkSettings"]["Networks"][name]
        member = self.request("GET", f"/networks/{nid}")["Containers"][cid]
        assert endpoint["NetworkID"] == nid, endpoint
        assert len(endpoint["EndpointID"]) == 64 and endpoint["EndpointID"] == member["EndpointID"], (endpoint, member)
        assert endpoint["Gateway"] and endpoint["MacAddress"] == member["MacAddress"], (endpoint, member)
        code, output = self.exec_capture(cid, "cat /sys/class/net/eth0/address; ip route")
        assert code == 0 and endpoint["MacAddress"].encode() in output and endpoint["Gateway"].encode() in output, output

    def http_attach_controls(self):
        cid, _ = self.run(["printf", "attach-history"])
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(10)
        try:
            sock.connect(self.path)
            path = f"/v1.48/containers/{cid}/attach?logs=1&stream=0&stdout=1&stderr=1"
            sock.sendall((f"POST {path} HTTP/1.1\r\nHost: localhost\r\nConnection: Upgrade\r\nUpgrade: tcp\r\nContent-Length: 0\r\n\r\n").encode())
            response = b""
            while True:
                data = sock.recv(65536)
                if not data: break
                response += data
            assert b"attach-history" in response, response
        finally:
            sock.close()
        cid = self.create(["cat"], OpenStdin=True, Tty=True)
        self.request("POST", f"/containers/{cid}/start", status=204)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(10)
        try:
            sock.connect(self.path)
            path = f"/v1.48/containers/{cid}/attach?stream=1&stdin=1&stdout=1&detachKeys=ctrl-a,ctrl-b"
            sock.sendall((f"POST {path} HTTP/1.1\r\nHost: localhost\r\nConnection: Upgrade\r\nUpgrade: tcp\r\nContent-Length: 0\r\n\r\n").encode())
            response = b""
            while b"\r\n\r\n" not in response: response += sock.recv(4096)
            sock.sendall(bytes([1])); time.sleep(.05); sock.sendall(bytes([2]))
            assert sock.recv(4096) == b"", "detach did not close stream"
        finally:
            sock.close()
        assert self.inspect(cid)["State"]["Running"], "detach stopped the process"

    def namespace_modes(self):
        cid = self.create(host={"NetworkMode": "none", "PidMode": "host", "IpcMode": "host", "UTSMode": "host", "CgroupnsMode": "host", "UsernsMode": "host"})
        self.request("POST", f"/containers/{cid}/start", status=204)
        code, output = self.exec_capture(cid, "for ns in pid ipc uts cgroup; do readlink /proc/self/ns/$ns; done")
        expected = [os.readlink("/proc/self/ns/" + ns) for ns in ["pid", "ipc", "uts", "cgroup"]]
        assert code == 0 and output.decode().splitlines() == expected, (code, output, expected)
        parent = self.create(host={"NetworkMode": "none", "IpcMode": "shareable"})
        self.request("POST", f"/containers/{parent}/start", status=204)
        assert self.exec_capture(parent, "printf shared > /dev/shm/compat")[0] == 0
        child = self.create(host={"NetworkMode": "none", "PidMode": "container:" + parent, "IpcMode": "container:" + parent})
        self.request("POST", f"/containers/{child}/start", status=204)
        for ns in ["pid", "ipc"]:
            assert self.exec_capture(child, "readlink /proc/self/ns/" + ns) == self.exec_capture(parent, "readlink /proc/self/ns/" + ns)
        assert self.exec_capture(child, "cat /dev/shm/compat") == (0, b"shared")

    def custom_seccomp(self):
        profile = {"defaultAction": "SCMP_ACT_ALLOW", "syscalls": [{"names": ["mkdir", "mkdirat"], "action": "SCMP_ACT_ERRNO", "errnoRet": 13}]}
        cid = self.create(host={"NetworkMode": "none", "SecurityOpt": ["seccomp=" + json.dumps(profile)]})
        self.request("POST", f"/containers/{cid}/start", status=204)
        code, output = self.exec_capture(cid, "mkdir /tmp/denied")
        assert code != 0 and b"Permission denied" in output, (code, output)

    def bridge_options(self):
        nid = self.network("-mtu", Options={"com.docker.network.driver.mtu": "1400", "com.docker.network.bridge.enable_icc": "false",
                                            "com.docker.network.bridge.host_binding_ipv4": "127.0.0.1", "com.docker.network.bridge.enable_ip_masquerade": "false"})
        name = self.request("GET", f"/networks/{nid}")["Name"]
        one = self.create(host={"NetworkMode": name, "PortBindings": {"8080/tcp": [{"HostPort": "0"}]}}, ExposedPorts={"8080/tcp": {}})
        two = self.create(host={"NetworkMode": name})
        for cid in [one, two]: self.request("POST", f"/containers/{cid}/start", status=204)
        assert self.exec_capture(one, "cat /sys/class/net/eth0/mtu") == (0, b"1400\n")
        binding = self.inspect(one)["NetworkSettings"]["Ports"]["8080/tcp"]
        assert all(b["HostIp"] == "127.0.0.1" for b in binding), binding
        address = self.inspect(two)["NetworkSettings"]["Networks"][name]["IPAddress"]
        assert self.exec_capture(one, "ping -c 1 -W 1 " + address)[0] != 0, "ICC=false allowed peer traffic"

    def source_nat(self):
        import urllib.request
        peer = os.environ["KIRI_TEST_PEER"]
        for enabled in [True, False]:
            nid = self.network("-nat-" + str(enabled), Options={"com.docker.network.bridge.enable_ip_masquerade": str(enabled).lower()})
            name = self.request("GET", f"/networks/{nid}")["Name"]
            cid = self.create(host={"NetworkMode": name})
            self.request("POST", f"/containers/{cid}/start", status=204)
            address = self.inspect(cid)["NetworkSettings"]["Networks"][name]["IPAddress"]
            token = uuid.uuid4().hex
            self.exec_capture(cid, "printf " + token + " | nc -u -w 1 " + peer + " 9000")
            observed = None
            for _ in range(50):
                with urllib.request.urlopen("http://" + peer + ":8000/" + token, timeout=5) as response:
                    observed = json.load(response)
                if observed: break
                time.sleep(.02)
            assert observed, "external observer received no packet"
            assert (observed != address) == enabled, (enabled, address, observed)

    def routed_network(self):
        nid = self.network("-routed", Options={"com.docker.network.bridge.gateway_mode_ipv4": "routed"})
        name = self.request("GET", f"/networks/{nid}")["Name"]
        command = "while true; do printf 'HTTP/1.0 200 OK\\r\\nContent-Length: 6\\r\\n\\r\\nrouted' | nc -l -p 8080; done"
        server = self.create(["sh", "-c", command], host={"NetworkMode": name, "PortBindings": {"8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "0"}]}}, ExposedPorts={"8080/tcp": {}})
        self.request("POST", f"/containers/{server}/start", status=204)
        details = self.inspect(server)["NetworkSettings"]
        assert details["Ports"]["8080/tcp"] == [{"HostIp": "0.0.0.0", "HostPort": ""}], details["Ports"]
        other = self.network("-routed-client")
        client = self.create(host={"NetworkMode": self.request("GET", f"/networks/{other}")["Name"]})
        self.request("POST", f"/containers/{client}/start", status=204)
        address = details["Networks"][name]["IPAddress"]
        code, output = self.exec_capture(client, "wget -qO- -T 3 http://" + address + ":8080/")
        assert code == 0 and b"routed" in output, (code, output)

    def direct_driver(self, driver):
        nid = self.network("-" + driver, Driver=driver, EnableIPv6=True, IPAM={"Config": [{"Subnet": "10.232." + ("44" if driver == "macvlan" else "45") + ".0/24"}, {"Subnet": "fd42:cafe:" + ("44" if driver == "macvlan" else "45") + "::/64"}]})
        name = self.request("GET", f"/networks/{nid}")["Name"]
        one, two = [self.create(host={"NetworkMode": name}) for _ in range(2)]
        for cid in [one, two]: self.request("POST", f"/containers/{cid}/start", status=204)
        address = self.inspect(two)["NetworkSettings"]["Networks"][name]["IPAddress"]
        result = self.exec_capture(one, "ping -c 1 -W 2 " + address)
        assert result[0] == 0, result
        address6 = self.inspect(two)["NetworkSettings"]["Networks"][name]["GlobalIPv6Address"]
        result = self.exec_capture(one, "ping -6 -c 1 -W 2 " + address6)
        assert result[0] == 0, result
        for cid in [one, two]:
            self.request("DELETE", f"/containers/{cid}?force=1", status=204)
            self.containers.remove(cid)
        self.request("DELETE", f"/networks/{nid}", status=204)
        self.networks.remove(nid)

    def benchmark(self, samples, concurrency):
        def once(_):
            started = time.perf_counter()
            cid = self.create(["true"], {"NetworkMode": "none"})
            try:
                self.request("POST", f"/containers/{cid}/start", status=204)
                assert self.request("POST", f"/containers/{cid}/wait")["StatusCode"] == 0
            finally:
                self.request("DELETE", f"/containers/{cid}", status=204)
                self.containers.remove(cid)
            return (time.perf_counter() - started) * 1000
        for _ in range(5):
            once(0)
        start = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            values = sorted(pool.map(once, range(samples)))
        return dict(samples=samples, concurrency=concurrency, network="none",
                    median_ms=statistics.median(values),
                    p95_ms=values[math.ceil(len(values) * .95) - 1],
                    p99_ms=values[math.ceil(len(values) * .99) - 1],
                    lifecycles_per_second=samples / (time.perf_counter() - start))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--image", default="alpine:3.21")
    parser.add_argument("--output", required=True)
    parser.add_argument("--benchmark", type=int, default=0)
    args = parser.parse_args()
    engine = Engine(args.socket, args.image)
    report = {"image": args.image}
    try:
        report["engine"] = engine.request("GET", "/version")
        data = engine.request("POST", "/images/create?fromImage=" + quote(args.image, safe=""), raw=True)
        for line in data.splitlines():
            value = json.loads(line)
            assert "error" not in value, value
        for name, fn in [
            ("advanced-resource-controls", engine.advanced_resources),
            ("endpoint-metadata", engine.endpoint_metadata),
            ("http-attach-controls", engine.http_attach_controls),
            ("namespace-modes", engine.namespace_modes),
            ("custom-seccomp", engine.custom_seccomp),
            ("bridge-options", engine.bridge_options),
            ("source-nat-toggle", engine.source_nat),
            ("routed-network", engine.routed_network),
            ("macvlan", lambda: engine.direct_driver("macvlan")),
            ("ipvlan", lambda: engine.direct_driver("ipvlan")),
            ("container-lifecycle", engine.lifecycle),
            ("pause-restart-detached-exec", engine.pause_restart_exec),
            ("resource-limit-reset", engine.resource_reset),
            ("invalid-updates-do-not-mutate", engine.invalid_updates),
            ("websocket-attach-roundtrip", engine.websocket_attach),
            ("websocket-legacy-text", lambda: engine.websocket_attach(version=27)),
            ("websocket-legacy-stream-defaults", lambda: engine.websocket_attach(version=41)),
            ("websocket-stdin-once", lambda: engine.websocket_attach(True)),
            ("ipv4-static-address", lambda: engine.addressing(True, False)),
            ("dual-stack-static-address", lambda: engine.addressing(True, True)),
            ("ipv6-only-static-address", lambda: engine.addressing(False, True)),
            ("connect-disconnect-ipv6-dns", engine.connect_disconnect),
        ]:
            engine.test(name, fn)
        if args.benchmark and all(r["passed"] for r in engine.results):
            report["benchmarks"] = [engine.benchmark(args.benchmark, c) for c in (1, 8)]
    finally:
        engine.cleanup()
        report["checks"] = engine.results
        with open(args.output, "w") as output:
            json.dump(report, output, indent=2)
    raise SystemExit(0 if all(r["passed"] for r in engine.results) else 1)


if __name__ == "__main__":
    main()
