"""Radiolack's tests. No test may read or write the owner's settings: the
test run gets an empty settings folder of its own through XDG_CONFIG_HOME
(store.py), which the programs it starts inherit."""

import atexit
import os
import shutil
import tempfile

_CONFIG_HOME = tempfile.mkdtemp(prefix="radiolack-tests-")
os.environ["XDG_CONFIG_HOME"] = _CONFIG_HOME
atexit.register(shutil.rmtree, _CONFIG_HOME, ignore_errors=True)
