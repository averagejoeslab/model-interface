"""Sandboxes: where tools run.

One container per session. It sees the session's workspace and nothing else: no network, no
other files, none of the platform's keys. The agent never reaches it except through a tool.
"""
import os
import re
import subprocess
from abc import ABC, abstractmethod

DEFAULT_IMAGE = "python:3.13@sha256:f758b6ca6fd1a733d6d50584acca528421c0d86d260107ede3f14ba1928b1343"
SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")  # safe as a container name and a folder name


class Sandbox(ABC):
    @abstractmethod
    def run(self, cmd: str, timeout: int = 60) -> str:
        """Run a command and return what it printed."""


class DockerSandbox(Sandbox):
    def __init__(self, session: str, image: str, workspaces: str):
        self.name = f"model-interface-{session}"
        self.image = image
        self.workspace = os.path.join(workspaces, session)

    def start(self):
        if subprocess.run(["docker", "inspect", self.name], capture_output=True).returncode == 0:
            return
        os.makedirs(self.workspace, exist_ok=True)
        subprocess.run(["docker", "run", "-d", "--name", self.name, "--network", "none",
                        "-v", f"{self.workspace}:/work", "-w", "/work", self.image, "sleep", "infinity"],
                       check=True, capture_output=True)

    def run(self, cmd, timeout=60):
        self.start()
        done = subprocess.run(["docker", "exec", self.name, "timeout", str(timeout), "sh", "-c", cmd],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
        if done.returncode == 124:
            return done.stdout + f"\n[stopped: ran longer than {timeout}s]"
        return done.stdout or f"(exit {done.returncode})"


class Sandboxes:
    """Sandboxes by session, made on first use."""

    def __init__(self, image: str = DEFAULT_IMAGE, workspaces: str = "~/.model-interface/workspaces", make=DockerSandbox):
        self.image = image
        self.workspaces = os.path.expanduser(workspaces)
        self.make = make
        self.open: dict[str, Sandbox] = {}

    @staticmethod
    def valid(session: str) -> bool:
        return SESSION.fullmatch(session) is not None

    def get(self, session: str) -> Sandbox:
        if not self.valid(session):
            raise ValueError(f"bad session name {session!r}")
        if session not in self.open:
            self.open[session] = self.make(session, self.image, self.workspaces)
        return self.open[session]
