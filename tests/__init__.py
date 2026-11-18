"""Radiolack's tests. No test may read or write the owner's settings,
recordings or GnuPG keys: the test run gets an empty folder of its own for
the settings, through XDG_CONFIG_HOME (store.py), and the recordings,
through XDG_DATA_HOME (recordings.py), and an empty GnuPG home in it through
GNUPGHOME, which the programs it starts inherit. The run removes the folder
when it ends.

No test may reach a real receiver through SoapySDR either: within the run,
``import SoapySDR`` finds only the stand-in (tests/soapysdr/SoapySDR.py,
``tests.fakes.stand_in_soapysdr``), and fails as a missing module
otherwise, even where the real module is installed."""

import atexit
import importlib.abc
import importlib.machinery
import os
import shutil
import sys
import tempfile
from pathlib import Path

# The run's folder, for the programs it starts that import this package too.
_RUN_FOLDER = "RADIOLACK_TEST_CONFIG_HOME"

if os.environ.get(_RUN_FOLDER):
    # Such as tests/fake_rtl_tcp.py: a folder of its own would stay behind
    # whenever a signal stops it, before it can remove it.
    os.environ.setdefault("XDG_CONFIG_HOME", os.environ[_RUN_FOLDER])
    os.environ.setdefault("XDG_DATA_HOME", os.environ[_RUN_FOLDER])
else:
    _CONFIG_HOME = tempfile.mkdtemp(prefix="radiolack-tests-")
    os.environ["XDG_CONFIG_HOME"] = os.environ["XDG_DATA_HOME"] = os.environ[_RUN_FOLDER] = _CONFIG_HOME
    atexit.register(shutil.rmtree, _CONFIG_HOME, ignore_errors=True)
    # Tests that sign make a GnuPG home of their own; this one only keeps
    # any other gpg away from the owner's keys.
    os.environ["GNUPGHOME"] = os.path.join(_CONFIG_HOME, "gnupg")
    os.mkdir(os.environ["GNUPGHOME"], 0o700)

_STAND_IN = Path(__file__).with_name("soapysdr")


class _StandInSoapySDROnly(importlib.abc.MetaPathFinder):
    """Finds ``SoapySDR`` only as the stand-in: the real module, found
    anywhere else on the path, would open the receivers plugged in."""

    def find_spec(self, name, path=None, target=None):
        if name != "SoapySDR":
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec is None or spec.origin is None or Path(spec.origin).parent != _STAND_IN:
            raise ModuleNotFoundError("the tests import only the stand-in SoapySDR (tests.fakes.stand_in_soapysdr)",
                                      name=name)
        return spec


sys.modules.pop("SoapySDR", None)
sys.meta_path.insert(0, _StandInSoapySDROnly())
