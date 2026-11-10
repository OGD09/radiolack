"""Radiolack's tests. No test may read or write the owner's settings: the
test run gets an empty settings folder of its own through XDG_CONFIG_HOME
(store.py), which the programs it starts inherit. The run removes the folder
when it ends."""

import atexit
import os
import shutil
import tempfile

# The run's folder, for the programs it starts that import this package too.
_RUN_FOLDER = "RADIOLACK_TEST_CONFIG_HOME"

if os.environ.get(_RUN_FOLDER):
    # Such as tests/fake_rtl_tcp.py: a folder of its own would stay behind
    # whenever a signal stops it, before it can remove it.
    os.environ.setdefault("XDG_CONFIG_HOME", os.environ[_RUN_FOLDER])
else:
    _CONFIG_HOME = tempfile.mkdtemp(prefix="radiolack-tests-")
    os.environ["XDG_CONFIG_HOME"] = os.environ[_RUN_FOLDER] = _CONFIG_HOME
    atexit.register(shutil.rmtree, _CONFIG_HOME, ignore_errors=True)
