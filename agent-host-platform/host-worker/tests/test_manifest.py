"""Manifest validation tests — PROTOCOL §4 rules."""
import pytest

from deployments.manifest import parse_memory_mb, validate_manifest


def valid_manifest():
    return {
        "name": "my-api",
        "runtime": "docker",
        "build": {"dockerfile": "Dockerfile", "context": "."},
        "service": {"port": 3000, "healthcheck": "/health"},
        "resources": {"memory": "1g", "cpu": 1},
        "restart": "unless-stopped",
        "env": {"NODE_ENV": "production"},
    }


def test_valid_manifest_passes():
    ok, errors = validate_manifest(valid_manifest(), project_name="my-api")
    assert ok, errors
    assert errors == []


def test_valid_manifest_without_project_name():
    ok, errors = validate_manifest(valid_manifest())
    assert ok, errors


def test_missing_name_fails():
    m = valid_manifest()
    del m["name"]
    ok, errors = validate_manifest(m)
    assert not ok
    assert any("name" in e for e in errors)


def test_empty_name_fails():
    m = valid_manifest()
    m["name"] = "   "
    ok, errors = validate_manifest(m)
    assert not ok


def test_name_mismatch_fails():
    ok, errors = validate_manifest(valid_manifest(), project_name="other-api")
    assert not ok
    assert any("match" in e for e in errors)


@pytest.mark.parametrize("runtime", ["docker", "docker-compose", "static"])
def test_valid_runtimes(runtime):
    m = valid_manifest()
    m["runtime"] = runtime
    ok, errors = validate_manifest(m)
    assert ok, errors


@pytest.mark.parametrize("runtime", ["k8s", "vm", "", None, "DOCKER"])
def test_bad_runtime_fails(runtime):
    m = valid_manifest()
    m["runtime"] = runtime
    ok, errors = validate_manifest(m)
    assert not ok
    assert any("runtime" in e for e in errors)


@pytest.mark.parametrize("port", [0, -1, 65536, 99999, "3000", 3.5, True])
def test_bad_port_fails(port):
    m = valid_manifest()
    m["service"]["port"] = port
    ok, errors = validate_manifest(m)
    assert not ok, f"port {port!r} should fail"
    assert any("service.port" in e for e in errors)


@pytest.mark.parametrize("port", [1, 80, 3000, 65535])
def test_good_ports_pass(port):
    m = valid_manifest()
    m["service"]["port"] = port
    ok, errors = validate_manifest(m)
    assert ok, errors


def test_missing_port_ok():
    m = valid_manifest()
    del m["service"]["port"]
    ok, errors = validate_manifest(m)
    assert ok, errors


@pytest.mark.parametrize("memory", ["256m", "1g", "512M", "2G", "1024m"])
def test_good_memory_passes(memory):
    m = valid_manifest()
    m["resources"]["memory"] = memory
    ok, errors = validate_manifest(m)
    assert ok, errors


@pytest.mark.parametrize("memory", ["1x", "1024", "1.5g", "1gb", "", "m", "-1g"])
def test_bad_memory_fails(memory):
    m = valid_manifest()
    m["resources"]["memory"] = memory
    ok, errors = validate_manifest(m)
    assert not ok, f"memory {memory!r} should fail"
    assert any("resources.memory" in e for e in errors)


@pytest.mark.parametrize("cpu", [0, -1, 0.0, "2", True])
def test_bad_cpu_fails(cpu):
    m = valid_manifest()
    m["resources"]["cpu"] = cpu
    ok, errors = validate_manifest(m)
    assert not ok, f"cpu {cpu!r} should fail"
    assert any("resources.cpu" in e for e in errors)


@pytest.mark.parametrize("cpu", [1, 2, 0.25, 16.0])
def test_good_cpu_passes(cpu):
    m = valid_manifest()
    m["resources"]["cpu"] = cpu
    ok, errors = validate_manifest(m)
    assert ok, errors


@pytest.mark.parametrize("restart", ["no", "always", "unless-stopped", "on-failure"])
def test_good_restart_passes(restart):
    m = valid_manifest()
    m["restart"] = restart
    ok, errors = validate_manifest(m)
    assert ok, errors


@pytest.mark.parametrize("restart", ["sometimes", "never", ""])
def test_bad_restart_fails(restart):
    m = valid_manifest()
    m["restart"] = restart
    ok, errors = validate_manifest(m)
    assert not ok, f"restart {restart!r} should fail"
    assert any("restart" in e for e in errors)


def test_bad_healthcheck_fails():
    m = valid_manifest()
    m["service"]["healthcheck"] = "health"  # missing leading slash
    ok, errors = validate_manifest(m)
    assert not ok
    assert any("healthcheck" in e for e in errors)


def test_bad_dockerfile_fails():
    m = valid_manifest()
    m["build"]["dockerfile"] = ""
    ok, errors = validate_manifest(m)
    assert not ok


def test_env_must_be_object():
    m = valid_manifest()
    m["env"] = ["NODE_ENV=production"]
    ok, errors = validate_manifest(m)
    assert not ok


def test_env_bad_values_fail():
    m = valid_manifest()
    m["env"] = {"OK": "yes", "BAD": {"nested": True}, "": "empty-key"}
    ok, errors = validate_manifest(m)
    assert not ok
    assert len(errors) == 2


def test_not_a_dict_fails():
    for bad in (None, [], "name", 42):
        ok, errors = validate_manifest(bad)
        assert not ok
        assert errors


def test_minimal_manifest_passes():
    ok, errors = validate_manifest({"name": "x", "runtime": "static"})
    assert ok, errors


def test_parse_memory_mb():
    assert parse_memory_mb("256m") == 256
    assert parse_memory_mb("1g") == 1024
    assert parse_memory_mb("2G") == 2048
    with pytest.raises(ValueError):
        parse_memory_mb("1.5g")
    with pytest.raises(ValueError):
        parse_memory_mb("nope")


# ---------------------------------------------------------------------------
# Spec §6 (WS3): manifest-level `volumes` is explicitly REJECTED — the
# docker runtime never mounts host paths, so accepting it would be
# persisted-but-inert partial support. Compose deployments carry their
# volumes in the compose file (validated by compose_validate.py).
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("volumes", [
    ["data:/data"],
    ["contract-data:/data", "logs:/var/log/app"],
    {"data": "/host/path"},
    "/host/data:/data",
    [],
])
def test_manifest_volumes_rejected(volumes):
    m = valid_manifest()
    m["volumes"] = volumes
    ok, errors = validate_manifest(m)
    assert not ok
    assert any("volumes" in e for e in errors)
    # the error points at the supported alternative
    assert any("docker-compose" in e for e in errors)


def test_manifest_volumes_absent_or_null_ok():
    m = valid_manifest()
    ok, errors = validate_manifest(m)
    assert ok, errors
    m["volumes"] = None  # null == absent
    ok, errors = validate_manifest(m)
    assert ok, errors


def test_manifest_volumes_rejected_for_compose_runtime_too():
    # Even compose deployments must not carry manifest-level volumes: the
    # compose file is the single source of truth for compose volumes.
    m = valid_manifest()
    m["runtime"] = "docker-compose"
    m["volumes"] = ["data:/data"]
    ok, errors = validate_manifest(m)
    assert not ok
    assert any("volumes" in e for e in errors)
