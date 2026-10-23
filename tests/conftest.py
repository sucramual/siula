"""Shared test helpers for black-box tests that run siula commands as subprocesses."""

import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
# The installed entry point, as a user runs it.
SIULA = Path(sys.executable).parent / "siula"

# A fake executable is a bash script: this header plus the body the test supplies.
FAKE_HEADER = "#!/usr/bin/env bash\n"

# The real tools that the scripts and fakes run. Each one reaches PATH only as a symlink in its own
# directory, never through a directory that also holds a real gh: when an exec fails, subprocess
# tries the next PATH entry, so a real gh later on PATH could post to GitHub.
REAL_TOOLS = ("bash", "git", "jq", "head", "cat", "wc")


@dataclass
class IsolatedEnv:
    """A minimal environment that keeps the user's HOME, Git config, and tools out of tests.

    ``env`` holds only HOME, PATH, GIT_CONFIG_GLOBAL, and GIT_CONFIG_NOSYSTEM, like ``env -i``.
    PATH is ``bin`` (the fakes), then ``tools`` (symlinks to the real tools in REAL_TOOLS and to
    this Python as python3). No PATH entry holds a real gh.
    """

    root: Path
    home: Path
    bin: Path
    tools: Path
    gitconfig: Path
    env: dict[str, str]

    def git(self, cwd: Path, *args: str) -> str:
        """Run git in cwd with this environment. Fails the test on a non-zero exit.

        Returns stripped stdout.
        """
        result = subprocess.run(
            ["git", *args], cwd=cwd, env=self.env, text=True, capture_output=True
        )
        if result.returncode != 0:
            pytest.fail(
                f"git {' '.join(args)} failed in {cwd} (exit {result.returncode}):\n{result.stderr}"
            )
        return result.stdout.strip()


@pytest.fixture
def isolated_env(tmp_path: Path) -> IsolatedEnv:
    """Temp HOME and global Git config (test identity, defaultBranch=main), plus a fakes dir.

    ``root`` is tmp_path with symlinks resolved (macOS /var -> /private/var), so it matches paths
    that tools and ``pwd -P`` report.
    """
    root = tmp_path.resolve()
    home, bin_dir, tools = root / "home", root / "bin", root / "tools"
    for directory in (home, bin_dir, tools):
        directory.mkdir()
    for name in REAL_TOOLS:
        found = shutil.which(name)
        if found is None:
            pytest.fail(f"the tests need {name} on PATH")
        (tools / name).symlink_to(found)
    (tools / "python3").symlink_to(sys.executable)
    assert not (tools / "gh").exists(), "a real gh must never be reachable from a test"
    gitconfig = root / "gitconfig"
    gitconfig.write_text(
        "[user]\n  email = test@example.com\n  name = Test\n[init]\n  defaultBranch = main\n"
    )
    env = {
        "HOME": str(home),
        "PATH": f"{bin_dir}{os.pathsep}{tools}",
        "GIT_CONFIG_GLOBAL": str(gitconfig),
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    return IsolatedEnv(root=root, home=home, bin=bin_dir, tools=tools, gitconfig=gitconfig, env=env)


@pytest.fixture
def fake_exe(isolated_env: IsolatedEnv) -> Callable[..., Path]:
    """Return ``make(name, body, directory=None) -> Path``, which writes an executable script.

    The script is ``#!/usr/bin/env bash`` plus ``body``. It goes in ``isolated_env.bin`` (first on
    PATH) unless ``directory`` is given; that directory is created if needed.
    """

    def make(name: str, body: str, directory: Path | None = None) -> Path:
        target_dir = directory or isolated_env.bin
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / name
        path.write_text(FAKE_HEADER + body)
        path.chmod(0o755)
        return path

    return make


@pytest.fixture
def run_tool(isolated_env: IsolatedEnv) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Return ``run(args, *, cwd, env=None, input=None, **extra_env) -> CompletedProcess[str]``.

    Runs a command as a subprocess and captures stdout and stderr as text. It never raises on a
    non-zero exit; assert on ``returncode``. ``env`` replaces ``isolated_env.env`` as the base,
    and ``extra_env`` entries are added on top (they may override PATH or HOME).
    """

    def run(
        args: Sequence[str | os.PathLike[str]],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        input: str | None = None,
        **extra_env: str,
    ) -> subprocess.CompletedProcess[str]:
        merged = {**(isolated_env.env if env is None else env), **extra_env}
        return subprocess.run(
            [os.fspath(arg) for arg in args],
            cwd=cwd,
            env=merged,
            input=input,
            text=True,
            capture_output=True,
            timeout=120,
        )

    return run
