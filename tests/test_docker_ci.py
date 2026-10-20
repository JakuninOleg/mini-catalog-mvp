import urllib.error
from pathlib import Path

import pytest

from tests import docker_ci


def test_ci_env_uses_example_and_refuses_overwrite(tmp_path, monkeypatch, capsys):
    example = (Path(__file__).resolve().parents[1] / ".env.example").read_text()
    monkeypatch.chdir(tmp_path)
    Path(".env.example").write_text(example)
    docker_ci.configure()
    values = dict(
        line.split("=", 1)
        for line in Path(".env").read_text().splitlines()
        if line and not line.startswith("#")
    )
    assert values["POSTGRES_DB"] == "catalog"
    assert values["DATABASE_URL"].endswith("@postgres:5432/catalog")
    assert values["POSTGRES_PASSWORD"] in values["DATABASE_URL"]
    assert values["MINIO_ROOT_PASSWORD"] == values["MINIO_SECRET_KEY"]
    assert values["TELEGRAM_ADMIN_ID"] == "42"
    assert "::add-mask::" + values["POSTGRES_PASSWORD"] in capsys.readouterr().out
    original = Path(".env").read_text()
    with pytest.raises(RuntimeError, match="overwrite"):
        docker_ci.configure()
    assert Path(".env").read_text() == original


def test_health_rejects_wrong_count_and_exited_process(monkeypatch):
    clock = iter([0, 0, 1, 2])
    monkeypatch.setattr(docker_ci.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(docker_ci.time, "sleep", lambda _: None)
    healthy = {"State": {"Status": "running", "Health": {"Status": "healthy"}}}
    monkeypatch.setattr(docker_ci, "containers", lambda _: [healthy] * 3)
    with pytest.raises(AssertionError, match="expected 2 healthy"):
        docker_ci.ready("backend", 2, timeout=2)
    monkeypatch.setattr(docker_ci.time, "monotonic", lambda: 0)
    monkeypatch.setattr(
        docker_ci,
        "containers",
        lambda _: [{"State": {"Status": "exited", "Health": {"Status": "healthy"}}}],
    )
    with pytest.raises(AssertionError, match="exited"):
        docker_ci.ready("backend", 2)


def test_routing_waits_through_transient_gateway_error(monkeypatch):
    expected = {"backend": {"10.0.0.2"}, "bot": {"10.0.0.3"}}
    monkeypatch.setattr(docker_ci, "dns", lambda service, port: expected[service])
    monkeypatch.setattr(docker_ci.time, "sleep", lambda _: None)
    calls = []

    def request(path, update=None):
        calls.append(path)
        if len(calls) == 1:
            raise urllib.error.HTTPError("http://proxy", 502, "restarting", {}, None)
        address = "10.0.0.3:8001" if update else "10.0.0.2:8000"
        return b"", {"X-Upstream": address}

    monkeypatch.setattr(docker_ci, "request", request)
    docker_ci.wait_routing(expected)
    assert len(calls) == 37


@pytest.mark.parametrize("stale_dns", [True, False])
def test_routing_rejects_stale_dns_or_upstream(monkeypatch, stale_dns):
    clock = iter([0, 0, 1, 2])
    monkeypatch.setattr(docker_ci.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(docker_ci.time, "sleep", lambda _: None)
    expected = {"backend": {"10.0.0.2"}, "bot": {"10.0.0.3"}}
    monkeypatch.setattr(
        docker_ci,
        "dns",
        lambda service, port: expected[service] | ({"10.0.0.99"} if stale_dns else set()),
    )
    monkeypatch.setattr(docker_ci, "request", lambda *args: (b"", {"X-Upstream": "10.0.0.99:8000"}))
    with pytest.raises(AssertionError, match="timed out"):
        docker_ci.wait_routing(expected, timeout=2)


@pytest.mark.parametrize("uid,mount_type", [("1000", "volume"), ("0", "volume"), ("1000", "bind")])
def test_minio_requires_uid_1000_and_named_volume(monkeypatch, uid, mount_type):
    monkeypatch.setattr(docker_ci, "run", lambda *args: uid)
    container = {
        "Id": "ci-minio",
        "Mounts": [{"Destination": "/data", "Type": mount_type, "RW": True, "Name": "ci-data"}],
    }
    if uid == "1000" and mount_type == "volume":
        assert docker_ci.minio_volume(container) == "ci-data"
    else:
        with pytest.raises(AssertionError):
            docker_ci.minio_volume(container)
