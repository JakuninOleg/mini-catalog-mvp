"""Docker integration orchestration on the GitHub runner; only Python stdlib required."""

import base64
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

COMPOSE = ["docker", "compose"]
PROXY = "http://127.0.0.1:18080"
COUNTS = {"postgres": 1, "minio": 1, "backend": 2, "bot": 2, "frontend": 1, "proxy": 1}


def configure():
    if Path(".env").exists():
        raise RuntimeError("Refusing to overwrite an existing .env")
    password, storage_password = secrets.token_hex(24), secrets.token_hex(24)
    values = {
        "POSTGRES_DB": "catalog",
        "POSTGRES_USER": "catalog_ci",
        "POSTGRES_PASSWORD": password,
        "DATABASE_URL": f"postgresql+asyncpg://catalog_ci:{password}@postgres:5432/catalog",
        "MINIO_ROOT_USER": "catalog-ci",
        "MINIO_ACCESS_KEY": "catalog-ci",
        "MINIO_ROOT_PASSWORD": storage_password,
        "MINIO_SECRET_KEY": storage_password,
        "MINIO_ENDPOINT": "minio:9000",
        "MINIO_BUCKET": "catalog",
        "MINIO_SECURE": "false",
        "BOT_API_SECRET": secrets.token_hex(24),
        "TELEGRAM_WEBHOOK_SECRET": secrets.token_hex(24),
        "BOT_TOKEN": "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk",
        "TELEGRAM_ADMIN_ID": "42",
        "MINI_APP_URL": "https://catalog.invalid",
        "BIND_ADDRESS": "127.0.0.1",
        "PORT": "18080",
    }
    example = Path(".env.example").read_text().splitlines()
    keys = {line.split("=", 1)[0] for line in example if line and not line.startswith("#")}
    assert keys == values.keys(), "Update the CI values when .env.example changes"
    for key, value in values.items():
        if any(word in key for word in ("PASSWORD", "SECRET", "TOKEN", "DATABASE_URL")):
            print(f"::add-mask::{value}", flush=True)
    Path(".env").write_text(
        "\n".join(
            f"{line.split('=', 1)[0]}={values[line.split('=', 1)[0]]}"
            if line and not line.startswith("#")
            else line
            for line in example
        )
        + "\n"
    )
    Path(".env").chmod(0o600)


def run(*command, capture=True):
    result = subprocess.run(command, check=True, text=True, capture_output=capture, timeout=600)
    return result.stdout.strip() if capture else ""


def compose(*args, capture=True):
    return run(*COMPOSE, *args, capture=capture)


def containers(service):
    ids = compose("ps", "-a", "-q", service).split()
    return json.loads(run("docker", "inspect", *ids)) if ids else []


def ready(service, count, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        instances = containers(service)
        if len(instances) == count and all(
            c["State"]["Status"] == "running"
            and c["State"].get("Health", {}).get("Status") == "healthy"
            for c in instances
        ):
            print(f"PASS: {service}: {count} healthy", flush=True)
            return instances
        if any(c["State"]["Status"] in {"exited", "dead"} for c in instances):
            raise AssertionError(f"{service} exited before becoming healthy")
        time.sleep(1)
    raise AssertionError(f"{service}: expected {count} healthy containers within {timeout}s")


def exec_backend(*args):
    return compose("exec", "-T", "--index", "1", "backend", *args)


def dns(service, port):
    return set(
        json.loads(
            exec_backend(
                "python",
                "-c",
                "import json; from scripts.check_replicas import addresses; "
                f"print(json.dumps(sorted(addresses('{service}', {port}))))",
            )
        )
    )


def ip(container):
    networks = container["NetworkSettings"]["Networks"]
    assert len(networks) == 1, "Expected the unchanged Compose default network"
    return next(iter(networks.values()))["IPAddress"]


def request(path, update=None):
    headers = {}
    data = None
    if update is not None:
        values = dict(
            line.split("=", 1)
            for line in Path(".env").read_text().splitlines()
            if line and not line.startswith("#")
        )
        data = json.dumps(update).encode()
        headers = {
            "Content-Type": "application/json",
            "X-Telegram-Bot-Api-Secret-Token": values["TELEGRAM_WEBHOOK_SECRET"],
        }
    with urllib.request.urlopen(
        urllib.request.Request(PROXY + path, data=data, headers=headers), timeout=6
    ) as response:
        return response.read(), response.headers


def wait_routing(expected, timeout=90):
    deadline = time.monotonic() + timeout
    last_error = "not ready"
    while time.monotonic() < deadline:
        try:
            assert dns("backend", 8000) == expected["backend"]
            assert dns("bot", 8001) == expected["bot"]
            seen = {"backend": set(), "bot": set()}
            for _ in range(12):
                for path in ("/health", "/api/products"):
                    _, headers = request(path)
                    assert headers["X-Upstream"] in {f"{ip}:8000" for ip in expected["backend"]}
                    seen["backend"].add(headers["X-Upstream"])
                _, headers = request("/telegram/webhook", {"update_id": 1})
                assert headers["X-Upstream"] in {f"{ip}:8001" for ip in expected["bot"]}
                seen["bot"].add(headers["X-Upstream"])
            for service, port in (("backend", 8000), ("bot", 8001)):
                assert seen[service] == {f"{ip}:{port}" for ip in expected[service]}
            print(f"PASS: Docker DNS and Nginx routing settled: {expected}", flush=True)
            return
        except (AssertionError, urllib.error.URLError, TimeoutError) as exc:
            last_error = type(exc).__name__
            time.sleep(1)
    raise AssertionError(f"Nginx/Docker DNS recovery timed out: {last_error}")


def minio_volume(container):
    mounts = [m for m in container["Mounts"] if m["Destination"] == "/data"]
    assert len(mounts) == 1 and mounts[0]["Type"] == "volume" and mounts[0]["RW"]
    assert run("docker", "exec", container["Id"], "id", "-u") == "1000"
    return mounts[0]["Name"]


def sql(database, query):
    return compose(
        "exec", "-T", "postgres", "psql", "-U", "catalog_ci", "-d", database, "-Atq", "-c", query
    )


def check():
    assert os.environ["COMPOSE_PROJECT_NAME"].startswith("mini-catalog-ci-"), "Use a CI project"
    running = {service: ready(service, count) for service, count in COUNTS.items()}
    for service in ("migrate", "storage-init"):
        instances = containers(service)
        assert len(instances) == 1 and instances[0]["State"]["Status"] == "exited"
        assert instances[0]["State"]["ExitCode"] == 0, f"{service} failed"
        print(f"PASS: {service} exited 0", flush=True)
    for service in ("backend", "bot", "frontend", "proxy"):
        for instance in running[service]:
            assert int(run("docker", "exec", instance["Id"], "id", "-u")) != 0
    assert dns("backend", 8000) == {ip(c) for c in running["backend"]}
    assert dns("bot", 8001) == {ip(c) for c in running["bot"]}
    print(exec_backend("python", "scripts/check_replicas.py", "--telegram-mock"), flush=True)
    state = json.loads(exec_backend("cat", "/tmp/replica-check.json"))

    removed = []
    survivors = {}
    for service in ("backend", "bot"):
        ordered = sorted(
            running[service],
            key=lambda c: int(c["Config"]["Labels"]["com.docker.compose.container-number"]),
        )
        survivors[service] = ip(ordered[0])
        removed.append(ordered[1]["Id"])
    run("docker", "stop", "--time", "10", *removed)
    run("docker", "rm", *removed)
    wait_routing({service: {address} for service, address in survivors.items()})
    assert state["payload"]["title"] in request("/")[0].decode()
    assert request(state["product"]["image_url"])[0] == base64.b64decode(
        state["payload"]["image_base64"]
    )
    compose("up", "-d", "--no-deps", "--scale", "backend=2", "--scale", "bot=2", "backend", "bot")
    ready("backend", 2)
    ready("bot", 2)
    # Wait for routing before strict replay; do not retry failed product writes.
    wait_routing({s: {ip(c) for c in containers(s)} for s in ("backend", "bot")})
    print(exec_backend("python", "scripts/check_replicas.py", "--verify-saved"), flush=True)

    original = running["minio"][0]
    volume = minio_volume(original)
    run(
        "docker",
        "exec",
        original["Id"],
        "sh",
        "-c",
        "test -w /data && printf volume-check > /data/.ci-write-check",
    )
    compose("up", "-d", "--no-deps", "--force-recreate", "minio")
    replacement = ready("minio", 1)[0]
    assert replacement["Id"] != original["Id"] and minio_volume(replacement) == volume
    assert (
        run("docker", "exec", replacement["Id"], "cat", "/data/.ci-write-check") == "volume-check"
    )
    ready("backend", 2)
    wait_routing({s: {ip(c) for c in containers(s)} for s in ("backend", "bot")})
    for product in (state["product"], state["webhook_product"]):
        assert request(product["image_url"])[0] == base64.b64decode(
            state["payload"]["image_base64"]
        )
    print(
        "PASS: MinIO UID 1000; writable named volume; PNGs survived container recreation",
        flush=True,
    )

    assert sql("postgres", "SELECT datname FROM pg_database WHERE datname = 'catalog_test'") == ""
    app_count = sql("catalog", "SELECT count(*) FROM products")
    assert int(app_count) == 2
    for attempt in (1, 2):
        compose("--profile", "test", "run", "--rm", "tests", capture=False)
        assert sql("catalog_test", "SELECT version_num FROM alembic_version") == "0001"
        assert sql("catalog", "SELECT count(*) FROM products") == app_count
        for product in (state["product"], state["webhook_product"]):
            assert request(product["image_url"])[0] == base64.b64decode(
                state["payload"]["image_base64"]
            )
        print(
            f"PASS: isolated catalog_test bootstrap/migrations/pytest #{attempt}; catalog intact",
            flush=True,
        )
    for service, count in COUNTS.items():
        ready(service, count)


if __name__ == "__main__":
    {"configure": configure, "check": check}[sys.argv[1]]()
