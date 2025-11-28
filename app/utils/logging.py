#!/usr/bin/env python3
"""
Logging configuration for the application.
"""

import logging
import os


def configure_logging(level: int = logging.INFO) -> None:
    """Configure application-wide logging."""
    logging.basicConfig(level=level)


def get_logger(name: str) -> logging.Logger:
    """Get a logger for the given name."""
    return logging.getLogger(name)


# Disable SSL warnings if configured
if os.getenv("PVE_VERIFY", "False").lower() not in ("true", "1", "yes"):
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
