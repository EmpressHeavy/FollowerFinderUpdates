"""Keep product tests off the license server.

The runners call license_check.require before a scan, suggestion, follow, or X change.
Those tests inject their own X data and never mean to open a subscription check.
test_license_check.py still runs the real gate.
"""
import inspect

from src import license_check as _lc

_real_require = _lc.require


def _require(action, *args, **kwargs):
    for frame in inspect.stack():
        if frame.filename.endswith("test_license_check.py"):
            return _real_require(action, *args, **kwargs)
    return {"allow": True, "reason": "test"}


_lc.require = _require
