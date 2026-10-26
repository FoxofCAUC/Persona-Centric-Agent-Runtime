"""Metadata-only logs for services, including third-party libraries."""
import json
import logging
from logging.handlers import RotatingFileHandler

from .models import timestamp


class MetadataFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({"at": timestamp(), "level": record.levelname,
                           "component": record.name, "event": getattr(record, "event_code", "library_event"),
                           "error_type": record.exc_info[0].__name__ if record.exc_info else None})


def setup(paths, component):
    directory = paths.home / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(directory / f"{component}.jsonl", maxBytes=5*1024*1024,
                                  backupCount=3, encoding="utf-8")
    handler.setFormatter(MetadataFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    # Remove upstream handlers that might write message bodies themselves.
    for logger in logging.Logger.manager.loggerDict.values():
        if isinstance(logger, logging.Logger):
            logger.handlers.clear()
            logger.propagate = True
