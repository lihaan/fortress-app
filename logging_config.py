"""
Logging configuration for the Fortress application.
"""

import logging
import sys
from pathlib import Path

LOG_FILE_PATH = Path(__file__).parent / "app.log"


def configure_logging() -> logging.Logger:
    """Configure logging to output to both console and app.log file.

    Returns:
        The root logger instance.
    """
    # Same format as beelink-app
    log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    date_format = "%Y-%m-%dT%H:%M:%S"

    # Create formatter
    formatter = logging.Formatter(log_format, datefmt=date_format)

    # Get root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    # Clear any existing handlers to avoid duplicates
    root_logger.handlers.clear()

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # File handler
    file_handler = logging.FileHandler(LOG_FILE_PATH, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    return root_logger


# Configure logging on module import
configure_logging()
