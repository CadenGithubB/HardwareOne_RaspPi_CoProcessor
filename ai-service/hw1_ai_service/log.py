"""Logging setup. Under systemd, journald stamps time/unit itself, so the
format stays bare; interactive runs get timestamps."""

import logging
import os
import sys


def setup(verbose: bool = False) -> None:
    under_systemd = bool(os.environ.get("INVOCATION_ID"))
    fmt = "%(levelname)s %(name)s: %(message)s" if under_systemd \
        else "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format=fmt,
        stream=sys.stderr,
    )
    # httpx request logging is noisy at INFO
    logging.getLogger("httpx").setLevel(logging.WARNING)
