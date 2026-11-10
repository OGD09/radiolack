"""Update check: whether a newer Radiolack is out, as its APT repository says.

Radiolack's Debian package adds its APT repository to the system, with the
repository's source (/etc/apt/sources.list.d/radiolack.sources) and key, so
that the system's software updates install new versions. The check asks
that repository for the latest version as the current user, leaving the
system's package lists alone: apt-get downloads the repository's signed
index into a temporary folder, with Radiolack's source alone and none of the
system's apt configuration (other repositories, proxies, hooks), then
apt-cache compares the latest version with the installed package's. The
check installs nothing: the system's software updates do.

Only a Radiolack that runs from the Debian package checks, with its source
in place and turned on; otherwise the check is unavailable, and says why. A
newer package installed since Radiolack started shows too, as Radiolack runs
it only once restarted.

The check is the only connection Radiolack makes besides the receiver and
its interfaces, and it carries no identifier: apt downloads the index as it
does from any repository, through the proxy that the environment names
(``https_proxy``), if any. On Debian 12, whose apt checks signatures through
apt-key, each check leaves an empty folder in /run/user/UID/gnupg, which the
end of the session removes.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import os
import pwd
import re
import signal
import tempfile
from pathlib import Path

from . import __version__

PACKAGE = "radiolack"
PACKAGE_DIR = Path("/usr/lib/python3/dist-packages/radiolack")    # where the Debian package installs Radiolack
APT_SOURCE = Path("/etc/apt/sources.list.d/radiolack.sources")    # the APT source that the package adds
TIMEOUT_S = 30.0        # a check that takes longer fails
# What the engine reports (docs/design/protocol.md); a check ends with one of the last five.
STATUSES = ("idle", "checking", "current", "available", "installed", "failed", "unavailable")

# apt reads this file first, and with these settings nothing of the system's
# configuration; the command line then sets the rest.
_APT_CONFIG = 'Dir::Etc::Main "/dev/null";\nDir::Etc::Parts "/dev/null";\n'
_VERSION_LINE = re.compile(r'^__version__ = "([^"]+)"$', re.MULTILINE)
_FALSE = {"no", "false", "off", "without", "disable", "0"}     # how apt may write false
_MAX_DETAIL = 300


@dataclasses.dataclass(frozen=True)
class UpdateResult:
    """What a check found: its ``status`` (one of STATUSES), the newer
    version, if any (``latest``), and why it failed or is unavailable
    (``detail``), in a sentence for the user."""

    status: str
    latest: str | None = None
    detail: str = ""


class UpdateCheck:
    """Asks Radiolack's APT repository whether a newer version is out.

    The paths and programs default to the system's: those of the Radiolack
    running (``version``, ``module_dir``), of its Debian package
    (``package_dir``, ``source``) and of dpkg's database (``status_file``,
    None for the system's). Tests give others."""

    def __init__(self, *, version: str = __version__, module_dir: Path = Path(__file__).parent,
                 package_dir: Path = PACKAGE_DIR, source: Path = APT_SOURCE, status_file: Path | None = None,
                 apt_get: str = "/usr/bin/apt-get", apt_cache: str = "/usr/bin/apt-cache",
                 dpkg: str = "/usr/bin/dpkg", timeout_s: float = TIMEOUT_S) -> None:
        self._version = version
        self._module_dir = module_dir
        self._package_dir = package_dir
        self._source = source
        self._status_file = status_file
        self._programs = {"apt-get": apt_get, "apt-cache": apt_cache, "dpkg": dpkg}
        self._timeout_s = timeout_s

    def unavailable_reason(self) -> str | None:
        """Why this Radiolack cannot check, in a sentence for the user, or
        None when it can."""
        try:
            packaged = self._module_dir.resolve() == self._package_dir.resolve()
        except (OSError, RuntimeError):     # such as a loop of symbolic links
            packaged = False
        if not packaged:
            return "Radiolack checks for updates when it runs from its Debian package."
        try:
            source = self._source.read_text(encoding="utf-8")
        except FileNotFoundError:
            return f"Radiolack's APT source is missing: {self._source}."
        except (OSError, UnicodeDecodeError) as error:
            return f"Radiolack's APT source cannot be read: {_reason(error)}."
        if _turned_off(source):
            return f"Radiolack's APT source is turned off: {self._source}."
        for name, program in self._programs.items():
            if not os.access(program, os.X_OK):
                return f"{name} is not installed."
        return None

    async def check(self) -> UpdateResult:
        """Ask the repository for the latest version. A check that fails or
        cannot run says so in its result; it raises only when cancelled,
        after stopping apt."""
        reason = self.unavailable_reason()
        if reason is not None:
            return UpdateResult("unavailable", detail=reason)
        deadline = asyncio.get_running_loop().time() + self._timeout_s
        try:
            with tempfile.TemporaryDirectory(prefix="radiolack-update-", ignore_cleanup_errors=True) as folder:
                return await self._check(Path(folder), deadline)
        except TimeoutError:        # before OSError, which includes it
            return UpdateResult("failed", detail=f"No answer within {self._timeout_s:g} s.")
        except OSError as error:
            return UpdateResult("failed", detail=f"apt could not run: {_reason(error)}.")

    async def _check(self, folder: Path, deadline: float) -> UpdateResult:
        lists, cache = folder / "lists", folder / "cache"
        (lists / "partial").mkdir(parents=True)
        (cache / "archives" / "partial").mkdir(parents=True)
        config = folder / "apt.conf"
        config.write_text(_APT_CONFIG, encoding="utf-8")
        environment = {**os.environ, "APT_CONFIG": str(config), "LC_ALL": "C"}
        settings = {
            "Dir::Etc::SourceList": str(self._source),
            "Dir::Etc::SourceParts": "/dev/null",
            "Dir::State::Lists": f"{lists}/",
            "Dir::Cache": f"{cache}/",
            "Debug::NoLocking": "true",
        }
        user = _user_name()
        if user is not None:
            # Run as root, apt would fetch as its _apt user, who cannot read the folder.
            settings["APT::Sandbox::User"] = user
        if self._status_file is not None:
            settings["Dir::State::status"] = str(self._status_file)
        options = [part for name, value in settings.items() for part in ("-o", f"{name}={value}")]

        code, output = await self._run(["apt-get", "-q", *options, "update"], environment, deadline)
        # apt-get reports some failures, such as an unreachable server, as warnings only.
        if code != 0 or not any(lists.glob("*_Packages")):
            return UpdateResult("failed", detail=_problem(output) or "The repository listed no packages.")
        code, output = await self._run(["apt-cache", *options, "policy", PACKAGE], environment, deadline)
        if code != 0:
            return UpdateResult("failed", detail=_problem(output) or f"apt-cache stopped with status {code}.")
        installed, candidate = _policy(output)
        if installed is None:
            return UpdateResult("unavailable", detail="Radiolack's Debian package is not installed.")
        if candidate is not None and candidate != installed:
            code, output = await self._run(["dpkg", "--compare-versions", candidate, "gt", installed],
                                           environment, deadline)
            if code not in (0, 1):
                return UpdateResult("failed", detail=_problem(output) or f"dpkg stopped with status {code}.")
            if code == 0:
                return UpdateResult("available", latest=candidate)
        on_disk = self._installed_version()
        if on_disk is not None and on_disk != self._version:
            return UpdateResult("installed", latest=on_disk)
        return UpdateResult("current")

    def _installed_version(self) -> str | None:
        """The version that the package installed, which runs at the next
        start, or None when it cannot be read."""
        try:
            text = (self._package_dir / "__init__.py").read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        match = _VERSION_LINE.search(text)
        return match.group(1) if match else None

    async def _run(self, command: list[str], environment: dict[str, str], deadline: float) -> tuple[int, str]:
        """Run one of the programs, named as in ``_programs``, until
        ``deadline`` (the event loop's time): its exit status and its output,
        both streams together. Raises OSError when it cannot start, and
        TimeoutError past the deadline; then, or when cancelled, it first
        stops the program and those it started, such as apt's download
        methods."""
        program, *arguments = command
        process = await asyncio.create_subprocess_exec(
            self._programs[program], *arguments, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=environment,
            start_new_session=True)
        try:
            remaining = deadline - asyncio.get_running_loop().time()
            output, _ = await asyncio.wait_for(process.communicate(), max(remaining, 0.0))
        except BaseException:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)      # its own session: the program and its children
            await process.wait()
            raise
        return process.returncode if process.returncode is not None else -1, output.decode("utf-8", "replace")


def _policy(output: str) -> tuple[str | None, str | None]:
    """The installed and candidate versions in apt-cache policy's output, as
    LC_ALL=C writes it; None for "(none)" or when it names neither."""
    found: dict[str, str | None] = {}
    for line in output.splitlines():
        name, separator, value = line.strip().partition(":")
        if separator and name in ("Installed", "Candidate") and name not in found:
            value = value.strip()
            found[name] = None if value in ("", "(none)") else value
    return found.get("Installed"), found.get("Candidate")


def _problem(output: str) -> str:
    """apt's first error or warning in its output, without its prefix, or
    ""."""
    for line in output.splitlines():
        if line.startswith(("E: ", "W: ")):
            text = " ".join(line[3:].split())
            return text if len(text) <= _MAX_DETAIL else text[:_MAX_DETAIL - 1] + "…"
    return ""


def _turned_off(source: str) -> bool:
    """Whether a deb822 APT source says ``Enabled: no``."""
    for line in source.splitlines():
        name, separator, value = line.partition(":")
        if (separator and not line[:1].isspace() and not line.startswith("#")
                and name.strip().lower() == "enabled" and value.strip().lower() in _FALSE):
            return True
    return False


def _user_name() -> str | None:
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return None


def _reason(error: OSError | UnicodeDecodeError) -> str:
    return (error.strerror if isinstance(error, OSError) else None) or str(error)
