from pathlib import Path

import pytest

from numbers_go_up.netfs import NetworkFilesystemError, check_not_network_filesystem

FIXTURES = Path(__file__).parent / "fixtures" / "mountinfo"


def test_local_filesystem_is_allowed():
    check_not_network_filesystem(
        "/data/stats.db", env={}, mountinfo_path=FIXTURES / "local_only.mountinfo"
    )


def test_nfs_mount_is_refused():
    with pytest.raises(NetworkFilesystemError, match="nfs4"):
        check_not_network_filesystem(
            "/data/stats.db", env={}, mountinfo_path=FIXTURES / "nfs_data.mountinfo"
        )


def test_nfs_override_warns_but_does_not_raise(caplog):
    check_not_network_filesystem(
        "/data/stats.db",
        env={"NGU_ALLOW_NETWORK_FS": "1"},
        mountinfo_path=FIXTURES / "nfs_data.mountinfo",
    )
    assert any("NGU_ALLOW_NETWORK_FS=1" in r.message for r in caplog.records)


def test_local_mount_nested_inside_nfs_resolves_to_local():
    # /mnt/nfs is nfs4, but /mnt/nfs/local is its own local ext4 mount --
    # the longest matching mount point must win.
    check_not_network_filesystem(
        "/mnt/nfs/local/data/stats.db",
        env={},
        mountinfo_path=FIXTURES / "nested_local_in_nfs.mountinfo",
    )


def test_path_still_inside_nfs_parent_is_refused():
    with pytest.raises(NetworkFilesystemError):
        check_not_network_filesystem(
            "/mnt/nfs/data/stats.db",
            env={},
            mountinfo_path=FIXTURES / "nested_local_in_nfs.mountinfo",
        )


def test_missing_proc_mountinfo_skips_with_a_warning(tmp_path, caplog):
    check_not_network_filesystem(
        "/data/stats.db", env={}, mountinfo_path=tmp_path / "does-not-exist"
    )
    assert any(
        "skipping the network-filesystem check" in r.message for r in caplog.records
    )
