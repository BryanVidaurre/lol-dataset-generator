"""Run all offline tests against disposable data, never the user dataset."""
import atexit
import logging
import os
import tempfile

_workspace = tempfile.TemporaryDirectory(prefix="lol-dataset-tests-")
os.environ["RIOT_DATA_DIR"] = os.path.join(_workspace.name, "data")
os.environ["RIOT_LOGS_DIR"] = os.path.join(_workspace.name, "logs")


def _cleanup_workspace():
    # Close Windows file handlers before removing their temporary directory.
    logging.shutdown()
    _workspace.cleanup()


atexit.register(_cleanup_workspace)
