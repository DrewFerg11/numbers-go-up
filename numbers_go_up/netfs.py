"""Refuse to run the database on a network filesystem.

SQLite's WAL locking is unreliable over NFS/SMB (``database.md``, "Failure
Handling"): two processes -- or even two mounts of the same share -- can
silently corrupt the file instead of erroring. This module finds the mount
that holds a given path and checks its filesystem type before migrations
run, so the failure happens loudly at startup rather than as a corrupted DB
weeks later.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

ENV_ALLOW_NETWORK_FS = "NGU_ALLOW_NETWORK_FS"

MOUNTINFO_PATH = "/proc/self/mountinfo"

# Filesystem types unsafe for SQLite's WAL locking.
_NETWORK_FSTYPES = {"nfs", "nfs4", "cifs", "smb3", "smbfs", "fuse.sshfs", "9p"}


class NetworkFilesystemError(Exception):
    """The database path resolves to a network filesystem mount."""


def _parse_mountinfo(text: str) -> list[tuple[str, str]]:
    """Return a list of (mount_point, fstype) from /proc/self/mountinfo content.

    Each line has a "-" separator; fields after it are
    "fstype source super_options". See proc(5).
    """
    mounts = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split(" - ", 1)
        if len(parts) != 2:
            continue
        pre, post = parts
        pre_fields = pre.split()
        post_fields = post.split()
        if len(pre_fields) < 5 or not post_fields:
            continue
        mount_point = pre_fields[4]
        fstype = post_fields[0]
        mounts.append((mount_point, fstype))
    return mounts


def _find_fstype(path: Path, mounts: list[tuple[str, str]]) -> str | None:
    """Longest matching mount point wins, so a local bind mounted inside a
    network share (or vice versa) resolves to the mount that actually
    backs it, not whichever line happened to come first."""
    resolved = str(path.resolve())
    best_match: tuple[str, str] | None = None
    for mount_point, fstype in mounts:
        matches = resolved == mount_point or resolved.startswith(
            mount_point.rstrip("/") + "/"
        )
        if matches and (best_match is None or len(mount_point) > len(best_match[0])):
            best_match = (mount_point, fstype)
    return best_match[1] if best_match else None


def check_not_network_filesystem(
    db_path: str | Path,
    env: dict[str, str] | None = None,
    mountinfo_path: str | Path = MOUNTINFO_PATH,
) -> None:
    """Raise NetworkFilesystemError if db_path sits on a network filesystem.

    Skips silently if mountinfo_path can't be read (non-Linux dev machines).
    The check can be overridden with NGU_ALLOW_NETWORK_FS=1, which still
    logs a WARNING on every start so the risk stays visible.
    """
    env = os.environ if env is None else env

    try:
        text = Path(mountinfo_path).read_text()
    except OSError:
        return

    mounts = _parse_mountinfo(text)
    directory = Path(db_path).parent
    fstype = _find_fstype(directory, mounts)
    if fstype is None or fstype not in _NETWORK_FSTYPES:
        return

    allowed = env.get(ENV_ALLOW_NETWORK_FS, "").strip() == "1"
    if allowed:
        logger.warning(
            "%s is on a %s network filesystem, but %s=1 overrides the refusal. "
            "SQLite's WAL locking is unreliable over network filesystems and "
            "can corrupt the database -- this is not a supported configuration.",
            directory,
            fstype,
            ENV_ALLOW_NETWORK_FS,
        )
        return

    raise NetworkFilesystemError(
        f"{directory} is on a {fstype} network filesystem. SQLite's WAL "
        f"locking is unreliable over NFS/SMB and can corrupt the database. "
        f"Move ./data to local disk. If you understand the risk and want to "
        f"proceed anyway, set {ENV_ALLOW_NETWORK_FS}=1."
    )
