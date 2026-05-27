"""
Local file cleanup module.
Removes temporary files from the output folder after successful cloud upload
to prevent disk space consumption.
"""

import logging
from pathlib import Path
from typing import Union

logger = logging.getLogger(__name__)


def remove_local_file(path: Union[str, Path]) -> bool:
    """
    Remove a single local file if it exists.
    Returns True if the file was removed (or did not exist).
    """
    p = Path(path)
    if not p.exists():
        return True
    if not p.is_file():
        logger.warning("Cleanup target is not a file, skipping: %s", p)
        return False
    try:
        p.unlink()
        logger.info("Cleaned up local file: %s", p)
        return True
    except OSError as exc:
        logger.warning("Failed to clean up local file %s: %s", p, exc)
        return False
