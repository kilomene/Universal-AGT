"""WS2 re-audit (§7): positional argv elements must never be flag-shaped.

The docker CLI (pflag) parses a positional argv element starting with "-"
as a FLAG. run()'s image already had the §17 guard; this pins the same
fail-closed guard on every other bare positional the client builds:

  * build(): context_dir is the last positional
    (`docker build -t <tag> -f <file> <context>`)
  * image_exists() / remove_image(): tag is the positional
    (`docker image inspect <tag>`, `docker rmi <tag>`)

Values consumed as a flag's own argument (-t/-f/-e/-p/--name) cannot hit
this ambiguity and need no guard.

No real docker daemon is touched: the client is constructed without
__init__ and _run is stubbed to capture the argv.
"""
import pytest

from docker import client as dc


def _client(captured):
    c = dc.DockerClient.__new__(dc.DockerClient)
    c.binary = "docker"

    def _run(*argv, **kwargs):
        captured.append(list(argv))

        class _Proc:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Proc()

    c._run = _run
    return c


def test_build_rejects_flag_shaped_context_dir():
    captured = []
    c = _client(captured)
    with pytest.raises(ValueError, match="must not start with"):
        c.build("--iidfile=/tmp/x", "/ctx/Dockerfile", "img:1")
    with pytest.raises(ValueError, match="must not start with"):
        c.build("-x", "/ctx/Dockerfile", "img:1")
    assert captured == []


def test_build_accepts_normal_context_dir():
    captured = []
    c = _client(captured)
    c.build("/ctx", "/ctx/Dockerfile", "img:1")
    assert captured and captured[0][-1] == "/ctx"


def test_image_exists_rejects_flag_shaped_tag():
    captured = []
    c = _client(captured)
    with pytest.raises(ValueError, match="must not start with"):
        c.image_exists("--help")
    with pytest.raises(ValueError, match="must not start with"):
        c.image_exists("-f")
    assert captured == []


def test_remove_image_rejects_flag_shaped_tag():
    captured = []
    c = _client(captured)
    with pytest.raises(ValueError, match="must not start with"):
        c.remove_image("--no-prune")
    assert captured == []


def test_image_tag_guards_accept_normal_tags():
    captured = []
    c = _client(captured)
    assert c.image_exists("uaht-app:v1") is True
    c.remove_image("uaht-app:v1")
    assert captured == [
        ["image", "inspect", "uaht-app:v1"],
        ["rmi", "uaht-app:v1"],
    ]


def test_run_image_guard_still_holds():
    # §17 guard (pre-existing): the image is the last positional of
    # `docker run` and must not be flag-shaped.
    captured = []
    c = _client(captured)
    with pytest.raises(ValueError, match="must not start with"):
        c.run("c1", "--privileged")
    assert captured == []
