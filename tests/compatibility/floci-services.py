"""Local Floci only: real Redis and PostgreSQL work, with verified responses."""

import argparse, json, math, socket, statistics, time, uuid
import boto3
import psycopg
from botocore.config import Config

p = argparse.ArgumentParser()
p.add_argument("--endpoint", default="http://floci:4566")
p.add_argument("--samples", type=int, default=10)
a = p.parse_args()


def client(service):
    return boto3.client(
        service,
        endpoint_url=a.endpoint,
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
        config=Config(
            read_timeout=180,
            connect_timeout=5,
            retries={"total_max_attempts": 1},
            proxies={},
        ),
    )


def elapsed(start):
    return (time.perf_counter() - start) * 1000


observations = {}
cache = client("elasticache")
rds = client("rds")
for service in ["elasticache", "rds"]:
    rows = []
    for sample in range(a.samples):
        name = "runtime-bench-" + uuid.uuid4().hex[:12]
        created = False
        row = {"resource_id": name}
        try:
            begin = time.perf_counter()
            if service == "elasticache":
                group = cache.create_replication_group(
                    ReplicationGroupId=name,
                    ReplicationGroupDescription="Runtime comparison",
                    Engine="redis",
                )["ReplicationGroup"]
                created = True
                ep = group["NodeGroups"][0]["PrimaryEndpoint"]
                row["create_api_ms"] = elapsed(begin)
                with socket.create_connection(
                    (ep["Address"], ep["Port"]), timeout=10
                ) as conn:
                    stream = conn.makefile("rb")
                    conn.sendall(b"*1\r\n$4\r\nPING\r\n")
                    assert stream.readline() == b"+PONG\r\n"
                    conn.sendall(b"*3\r\n$3\r\nSET\r\n$3\r\nkey\r\n$5\r\nvalue\r\n")
                    assert stream.readline() == b"+OK\r\n"
                    conn.sendall(b"*2\r\n$3\r\nGET\r\n$3\r\nkey\r\n")
                    assert (
                        stream.readline() == b"$5\r\n"
                        and stream.read(7) == b"value\r\n"
                    )
            else:
                db = rds.create_db_instance(
                    DBInstanceIdentifier=name,
                    DBInstanceClass="db.t3.micro",
                    Engine="postgres",
                    MasterUsername="admin",
                    MasterUserPassword="secret123",
                    DBName="app",
                    AllocatedStorage=20,
                )["DBInstance"]
                created = True
                ep = db["Endpoint"]
                row["create_api_ms"] = elapsed(begin)
                with psycopg.connect(
                    host=ep["Address"],
                    port=ep["Port"],
                    user="admin",
                    password="secret123",
                    dbname="app",
                    connect_timeout=10,
                ) as conn:
                    with conn.cursor() as cursor:
                        cursor.execute("CREATE TABLE benchmark (value integer)")
                        cursor.execute("INSERT INTO benchmark VALUES (42)")
                        cursor.execute("SELECT value FROM benchmark")
                        assert cursor.fetchone() == (42,)
            row["create_to_verified_result_ms"] = elapsed(begin)
        finally:
            if created:
                start = time.perf_counter()
                if service == "elasticache":
                    cache.delete_replication_group(ReplicationGroupId=name)
                else:
                    rds.delete_db_instance(
                        DBInstanceIdentifier=name, SkipFinalSnapshot=True
                    )
                row["delete_ms"] = elapsed(start)
        row["lifecycle_ms"] = elapsed(begin)
        rows.append(row)
    observations[service] = {
        "samples": rows,
        "summary": {
            key: {
                "n": len(rows),
                "median": statistics.median(r[key] for r in rows),
                "p95": sorted(r[key] for r in rows)[math.ceil(len(rows) * 0.95) - 1],
            }
            for key in rows[0]
            if key.endswith("_ms")
        },
    }
print(json.dumps(observations, indent=2))
