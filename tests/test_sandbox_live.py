"""DockerSandbox against a real daemon. Skipped where there isn't one."""
import os
import subprocess

import pytest

from model_interface.sandbox import Sandboxes

docker = subprocess.run(["docker", "info"], capture_output=True).returncode == 0
pytestmark = pytest.mark.skipif(not docker, reason="no Docker daemon")


@pytest.fixture
def boxes(tmp_path):
    boxes = Sandboxes(workspaces=str(tmp_path))
    yield boxes
    for session in boxes.open:
        subprocess.run(["docker", "rm", "-f", f"model-interface-{session}"], capture_output=True)


def test_a_command_runs_in_the_workspace_and_its_files_persist(boxes, tmp_path):
    box = boxes.get("live-a")
    assert box.run("pwd") == "/work\n"
    assert box.run("echo kept > note.txt && cat note.txt") == "kept\n"
    assert (tmp_path / "live-a" / "note.txt").read_text() == "kept\n"


def test_the_sandbox_has_no_network(boxes):
    out = boxes.get("live-a").run("python3 -c 'import urllib.request; urllib.request.urlopen(\"https://pypi.org\", timeout=3)' 2>&1")
    assert "URLError" in out


def test_errors_and_exit_codes_come_back_as_text(boxes):
    box = boxes.get("live-a")
    assert box.run("false") == "(exit 1)"
    assert "No such file" in box.run("ls /nope")


def test_a_long_command_is_stopped_at_the_timeout(boxes):
    assert boxes.get("live-a").run("sleep 5; echo late", timeout=1).endswith("[stopped: ran longer than 1s]")


def test_sessions_do_not_see_each_other(boxes):
    boxes.get("live-a").run("echo mine > note.txt")
    assert boxes.get("live-b").run("ls") == "(exit 0)"
