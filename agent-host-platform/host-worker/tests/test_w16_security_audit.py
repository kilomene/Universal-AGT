"""W16 FINAL SECURITY AUDIT — worker-side adversarial tests.

Each test is an ATTACK ATTEMPT first, a regression guard second. Findings:

  W1  compose namespace sharing (deployments/compose_validate.py):
      `network_mode: "container:<any>"`, `pid: "container:<any>"`,
      `ipc: "container:<any>"` joined the target container's namespaces and
      were NOT denied (only the literal "host" was). Demonstrated below,
      now denied.
  W2  manifest validation gaps (deployments/manifest.py):
      - `resources.memory` with a 5000-digit number passed the regex and
        then crashed int() (Python 3.11+ caps str->int at 4300 digits)
        deep in the deploy pipeline.
      - `resources.cpu: NaN` (JSON-parseable by Python's json) passed the
        "positive number" check.
      Both demonstrated below, now rejected at the validation boundary.
"""
import io
import json
import math
import os
import sys
import tarfile
import tempfile
import zipfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from deployments import compose_validate, manifest, pipeline
from deployments.manifest import parse_memory_mb, validate_manifest
from docker.client import (
    _BUILD_ARG_KEY_RE,
    sanitize_ident,
    validate_build_args,
    validate_run_args,
)


@pytest.fixture()
def ws(tmp_path):
    d = tmp_path / "workspace"
    d.mkdir()
    return d


def svc_inner(**kw):
    m = {"image": "example.com/app:1"}
    m.update(kw)
    return m


def model(**kw):
    return {"services": {"web": svc_inner(**kw)}}


# ---------------------------------------------------------------------------
# W1 — compose namespace sharing
# ---------------------------------------------------------------------------

class TestNamespaceSharing:
    @pytest.mark.parametrize("key", ["network_mode", "pid", "ipc"])
    @pytest.mark.parametrize("value", [
        "container:uaht-victim",
        "container:host-infra",
        "CONTAINER:UPPER",
        "container:abc123",
    ])
    def test_container_namespace_join_denied(self, ws, key, value):
        """An agent compose file must not join another container's namespaces."""
        errors = compose_validate.validate_compose_model(model(**{key: value}), ws)
        assert any("namespace sharing" in e for e in errors), (
            f"W1 REGRESSION: {key}={value!r} passed validation: {errors}"
        )

    def test_service_namespace_join_still_allowed(self, ws):
        # `service:<name>` references a sibling service in the SAME compose
        # file — the agent already fully controls that project.
        assert compose_validate.validate_compose_model(
            model(network_mode="service:sibling"), ws) == []

    def test_host_denials_intact(self, ws):
        for key, value in [("network_mode", "host"), ("pid", "host"),
                           ("ipc", "host"), ("uts", "host")]:
            errors = compose_validate.validate_compose_model(model(**{key: value}), ws)
            assert errors, f"{key}={value} should still be denied"

    def test_privileged_buried_in_x_extension_is_inert(self, ws):
        # x- extension fields are IGNORED by compose at runtime (compose
        # spec): burying `privileged` there cannot affect the container.
        # The validator correctly ignores x- keys — this pins that behavior
        # so a future "fix" doesn't start executing extension content.
        m = model()
        m["x-evil"] = {"privileged": True}
        m["services"]["web"]["x-also-evil"] = {"privileged": True}
        assert compose_validate.validate_compose_model(m, ws) == []

    def test_privileged_via_extends_merge_is_caught(self, ws):
        # `extends` is resolved by `docker compose config` BEFORE
        # validation, so a privileged base service appears as
        # privileged: true in the NORMALIZED model the validator sees.
        errors = compose_validate.validate_compose_model(
            model(privileged=True), ws)
        assert any("privileged" in e for e in errors)

    def test_cap_add_still_denied(self, ws):
        errors = compose_validate.validate_compose_model(
            model(cap_add=["SYS_ADMIN", "NET_ADMIN"]), ws)
        assert any("cap_add" in e for e in errors)

    def test_devices_still_denied(self, ws):
        errors = compose_validate.validate_compose_model(
            model(devices=["/dev/kvm"]), ws)
        assert any("devices" in e for e in errors)


# ---------------------------------------------------------------------------
# W2 — manifest validation hardening
# ---------------------------------------------------------------------------

class TestManifestHardening:
    def test_giant_memory_rejected_at_boundary(self):
        """5000-digit memory passed the old regex then crashed int()."""
        big = "9" * 5000 + "m"
        ok, errors = validate_manifest(
            {"name": "x", "runtime": "docker", "resources": {"memory": big}})
        assert not ok
        assert any("memory" in e for e in errors)

    def test_parse_memory_mb_never_raises_cryptically(self):
        for bad in ["9" * 5000 + "m", "", "m", "1x", "-1g", None, 123]:
            with pytest.raises(ValueError):
                parse_memory_mb(bad)

    def test_parse_memory_mb_valid(self):
        assert parse_memory_mb("256m") == 256
        assert parse_memory_mb("1g") == 1024
        assert parse_memory_mb("2G") == 2048
        assert parse_memory_mb("999999m") == 999999

    def test_nan_cpu_rejected(self):
        ok, errors = validate_manifest(
            {"name": "x", "runtime": "docker", "resources": {"cpu": float("nan")}})
        assert not ok
        assert any("cpu" in e for e in errors)

    def test_inf_cpu_rejected(self):
        ok, errors = validate_manifest(
            {"name": "x", "runtime": "docker", "resources": {"cpu": float("inf")}})
        assert not ok

    def test_sane_resources_still_pass(self):
        ok, errors = validate_manifest({
            "name": "x", "runtime": "docker",
            "resources": {"memory": "512m", "cpu": 1.5},
        })
        assert ok, errors

    @pytest.mark.parametrize("bad", [
        None, 42, "string", [], True,
        {"name": ""}, {"name": "x", "runtime": "bogus"},
        {"name": "x", "runtime": "docker", "resources": {"memory": "1.5g"}},
        {"name": "x", "runtime": "docker", "service": {"port": "80"}},
        {"name": "x", "runtime": "docker", "service": {"port": 0}},
        {"name": "x", "runtime": "docker", "service": {"port": 70000}},
        {"name": "x", "runtime": "docker", "service": {"port": True}},
        {"name": "x", "runtime": "docker", "env": {"K": {"nested": 1}}},
        {"name": "x", "runtime": "docker", "env": {"K": None}},
        {"name": "x", "runtime": "docker", "restart": "sometimes"},
        {"name": "x", "runtime": "docker", "resources": {"cpu": "lots"}},
        {"name": "x", "runtime": "docker", "resources": {"cpu": -1}},
        {"name": "x", "runtime": "docker", "build": {"dockerfile": ""}},
        {"name": "mismatch", "runtime": "docker"},
    ])
    def test_malformed_manifests_rejected_not_crashing(self, bad):
        """Fuzz: the validator must return (False, errors), never raise."""
        try:
            ok, errors = validate_manifest(bad, project_name="x" if isinstance(bad, dict) and bad.get("name") == "mismatch" else None)
        except Exception as exc:
            pytest.fail(f"validate_manifest raised on {bad!r}: {exc!r}")
        assert not ok, f"expected rejection of {bad!r}"
        assert errors

    def test_deeply_nested_manifest_does_not_raise(self):
        nested: dict = {}
        cur = nested
        for _ in range(500):
            cur["x"] = {}
            cur = cur["x"]
        ok, _ = validate_manifest({"name": "x", "runtime": "docker", "env": nested})
        assert not ok  # env values must be scalars — rejected, not crashed


# ---------------------------------------------------------------------------
# Filesystem: archive traversal — tricky members
# ---------------------------------------------------------------------------

def _make_tar(path, members):
    with tarfile.open(path, "w:gz") as tf:
        for name, kind in members:
            if kind == "file":
                data = b"hello"
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
            elif kind == "symlink":
                info = tarfile.TarInfo(name)
                info.type = tarfile.SYMTYPE
                info.linkname = "/etc/passwd"
                tf.addfile(info)
            elif kind == "dir":
                info = tarfile.TarInfo(name)
                info.type = tarfile.DIRTYPE
                tf.addfile(info)


def _make_zip(path, names):
    with zipfile.ZipFile(path, "w") as zf:
        for name in names:
            zf.writestr(name, b"hello")


class TestArchiveTraversal:
    @pytest.mark.parametrize("member", [
        "../evil.txt",
        "a/../../evil.txt",
        "/etc/cron.d/evil",
        "/tmp/../etc/evil",
    ])
    def test_tar_tricky_members(self, tmp_path, member):
        arch = tmp_path / "evil.tar.gz"
        _make_tar(str(arch), [(member, "file")])
        with pytest.raises(Exception):
            pipeline.extract_archive(str(arch), str(tmp_path / "out"))

    def test_tar_literal_dot_names_stay_inside(self, tmp_path):
        """'....' is a legal filename, not '..' — it must extract INSIDE."""
        arch = tmp_path / "dots.tar.gz"
        _make_tar(str(arch), [("....//ok.txt", "file")])
        out = tmp_path / "out"
        pipeline.extract_archive(str(arch), str(out))
        assert (out / "...." / "ok.txt").exists()

    def test_tar_symlink_to_outside_blocked(self, tmp_path):
        arch = tmp_path / "link.tar.gz"
        _make_tar(str(arch), [("link", "symlink"), ("link/evil.txt", "file")])
        with pytest.raises(Exception):
            pipeline.extract_archive(str(arch), str(tmp_path / "out"))

    def test_tar_absolute_symlink_member_blocked(self, tmp_path):
        arch = tmp_path / "abs.tar.gz"
        with tarfile.open(arch, "w:gz") as tf:
            info = tarfile.TarInfo("/abs.txt")
            info.size = 5
            tf.addfile(info, io.BytesIO(b"hello"))
        with pytest.raises(Exception):
            pipeline.extract_archive(str(arch), str(tmp_path / "out"))

    @pytest.mark.parametrize("member", [
        "../evil.txt",
        "a/../../evil.txt",
        "/etc/evil.txt",
    ])
    def test_zip_tricky_members_blocked(self, tmp_path, member):
        arch = tmp_path / "evil.zip"
        _make_zip(str(arch), [member])
        with pytest.raises(Exception):
            pipeline.extract_archive(str(arch), str(tmp_path / "out"))

    def test_zip_unicode_dotdot_blocked(self, tmp_path):
        # Fullwidth full stop (U+FF0E) is NOT ".." — legal name, stays inside.
        # Real ".." with unicode normalization tricks still resolves out.
        arch = tmp_path / "uni.zip"
        _make_zip(str(arch), ["\u2024\u2024/evil.txt"])  # ONE DOT LEADER x2
        out = tmp_path / "out"
        pipeline.extract_archive(str(arch), str(out))
        assert (out / "\u2024\u2024" / "evil.txt").exists()

    def test_zip_benign_extracts(self, tmp_path):
        arch = tmp_path / "ok.zip"
        _make_zip(str(arch), ["app/main.py", "agent.deploy.json"])
        out = tmp_path / "out"
        pipeline.extract_archive(str(arch), str(out))
        assert (out / "app" / "main.py").exists()


# ---------------------------------------------------------------------------
# Docker argv construction: no shell, no flag smuggling
# ---------------------------------------------------------------------------

class TestDockerArgv:
    def test_env_value_that_looks_like_flag_stays_a_value(self):
        # validate_run_args passes env through; run() puts each as ONE argv
        # element after -e (no shell). The dangerous shape would be a KEY
        # smuggling a flag — keys are restricted by _BUILD_ARG_KEY_RE for
        # build args; env keys go through sanitize_env ([A-Za-z_][A-Za-z0-9_]*).
        validate_run_args(ports={"8080": 80}, memory="256m", cpus="1.5",
                          restart="unless-stopped")

    @pytest.mark.parametrize("kwargs", [
        {"restart": "--privileged"},
        {"restart": "always; rm -rf /"},
        {"memory": "256m --privileged"},
        {"memory": "1g;evil"},
        {"cpus": "1 --privileged"},
        {"ports": {"8080;evil": 80}},
        {"ports": {"8080": "80 --privileged"}},
        {"ports": {"0": 80}},
        {"ports": {"8080": 70000}},
    ])
    def test_validate_run_args_rejects_hostile(self, kwargs):
        with pytest.raises(ValueError):
            validate_run_args(**kwargs)

    def test_sanitize_ident_neutralizes(self):
        assert sanitize_ident("--privileged") == "privileged"
        assert sanitize_ident("a; rm -rf /") == "a-rm--rf"
        assert sanitize_ident("") == "unnamed"
        assert len(sanitize_ident("x" * 500)) <= 120

    def test_build_arg_key_cannot_smuggle_flag(self):
        with pytest.raises(ValueError):
            validate_build_args({"--privileged": "1"})
        with pytest.raises(ValueError):
            validate_build_args({"K;evil": "1"})
        validate_build_args({"GOOD_KEY": "1"})
