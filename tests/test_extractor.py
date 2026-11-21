"""Tests for the extraction engine."""

from __future__ import annotations

import os
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

from unzipfast.archive import build_plan
from unzipfast.errors import PasswordError
from unzipfast.extractor import choose_workers, extract_archive, verify_archive


def make_zip(path: Path, files: dict[str, bytes], *, stored: bool = False) -> Path:
    method = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(path, "w", method) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return path


def run(archive: Path, destination: Path, *, jobs: int = 1, **plan_kwargs):
    plan = build_plan(archive, destination, **plan_kwargs)
    result = extract_archive(plan, archive, jobs=jobs)
    return plan, result


def test_extract_nested_tree(tmp_path: Path) -> None:
    archive = make_zip(
        tmp_path / "tree.zip",
        {
            "a.txt": b"hello",
            "dir/b.txt": b"world",
            "dir/sub/c.bin": bytes(range(256)),
        },
    )
    destination = tmp_path / "out"

    _, result = run(archive, destination)

    assert result.files == 3
    assert result.errors == []
    assert (destination / "a.txt").read_bytes() == b"hello"
    assert (destination / "dir" / "b.txt").read_bytes() == b"world"
    assert (destination / "dir" / "sub" / "c.bin").read_bytes() == bytes(range(256))


def test_extract_parallel_matches_serial(tmp_path: Path) -> None:
    files = {f"group/{index:03d}.bin": os.urandom(2048) for index in range(60)}
    archive = make_zip(tmp_path / "many.zip", files)

    serial_dir = tmp_path / "serial"
    parallel_dir = tmp_path / "parallel"
    _, serial = run(archive, serial_dir, jobs=1)
    _, parallel = run(archive, parallel_dir, jobs=4)

    assert serial.files == parallel.files == 60
    assert serial.errors == parallel.errors == []
    for name, data in files.items():
        assert (serial_dir / name).read_bytes() == data
        assert (parallel_dir / name).read_bytes() == data


def test_extract_flat_mode(tmp_path: Path) -> None:
    archive = make_zip(
        tmp_path / "flat.zip",
        {"one/a.txt": b"1", "two/b.txt": b"2"},
    )
    destination = tmp_path / "out"

    run(archive, destination, flat=True)

    assert sorted(path.name for path in destination.iterdir()) == ["a.txt", "b.txt"]


def test_extract_creates_empty_directories(tmp_path: Path) -> None:
    archive = tmp_path / "empty-dir.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("empty/", b"")

    run(archive, tmp_path / "out")

    assert (tmp_path / "out" / "empty").is_dir()


def test_extract_skips_existing_by_default(tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "a.zip", {"a.txt": b"new"})
    destination = tmp_path / "out"
    destination.mkdir()
    (destination / "a.txt").write_bytes(b"old")

    _, result = run(archive, destination)

    assert result.files == 0
    assert (destination / "a.txt").read_bytes() == b"old"


def test_extract_overwrites_when_asked(tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "a.zip", {"a.txt": b"new"})
    destination = tmp_path / "out"
    destination.mkdir()
    (destination / "a.txt").write_bytes(b"old")

    run(archive, destination, overwrite="overwrite")

    assert (destination / "a.txt").read_bytes() == b"new"


def test_extract_preserves_mtime(tmp_path: Path) -> None:
    archive = tmp_path / "time.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        info = zipfile.ZipInfo("stamped.txt", date_time=(2020, 1, 2, 3, 4, 6))
        zf.writestr(info, b"data")

    run(archive, tmp_path / "out")

    expected = datetime(2020, 1, 2, 3, 4, 6).timestamp()
    actual = (tmp_path / "out" / "stamped.txt").stat().st_mtime
    assert abs(actual - expected) < 2


def test_corrupted_member_is_reported(tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "bad.zip", {"data.txt": b"hello world"}, stored=True)
    raw = bytearray(archive.read_bytes())
    offset = raw.find(b"hello world")
    assert offset > 0
    raw[offset] = ord("H")
    archive.write_bytes(bytes(raw))

    plan = build_plan(archive, tmp_path / "out")
    result = extract_archive(plan, archive, jobs=1)

    assert result.files == 0
    assert len(result.errors) == 1
    assert "CRC" in result.errors[0][1] or "BadZipFile" in result.errors[0][1]
    assert not (tmp_path / "out" / "data.txt").exists()
    assert not list((tmp_path / "out").glob("*.part"))


def test_verify_archive_detects_corruption(tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "bad.zip", {"data.txt": b"hello world"}, stored=True)
    raw = bytearray(archive.read_bytes())
    offset = raw.find(b"hello world")
    raw[offset] = ord("H")
    archive.write_bytes(bytes(raw))

    checked, failures = verify_archive(archive, None)

    assert checked == 0
    assert len(failures) == 1
    assert failures[0][0] == "data.txt"


def test_verify_archive_accepts_good_archive(tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "good.zip", {"a.txt": b"a" * 1000, "b.txt": b"b"})
    checked, failures = verify_archive(archive, None)
    assert checked == 2
    assert failures == []


def test_encrypted_archive_needs_correct_password(tmp_path: Path) -> None:
    pyzipper = pytest.importorskip("pyzipper")
    archive = tmp_path / "secret.zip"
    with pyzipper.AESZipFile(
        archive, "w", compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES
    ) as zf:
        zf.setpassword(b"correct horse")
        zf.writestr("secret.txt", b"top secret")

    plan = build_plan(archive, tmp_path / "out")

    with pytest.raises(PasswordError):
        extract_archive(plan, archive, jobs=1, passwords=["wrong"])

    with pytest.raises(PasswordError):
        extract_archive(plan, archive, jobs=1)

    result = extract_archive(plan, archive, jobs=1, passwords=["correct horse"])
    assert result.files == 1
    assert (tmp_path / "out" / "secret.txt").read_bytes() == b"top secret"


def test_choose_workers() -> None:
    # Explicit values are clamped to the number of files.
    assert choose_workers(4, 100, 10**9) == 4
    assert choose_workers(64, 3, 10**9) == 3
    # Auto mode stays serial for tiny archives (process spawn is not free).
    assert choose_workers(0, 20, 18 * 1024) == 1
    assert choose_workers(0, 10, 100 * 1024 * 1024) == 1  # only 10 files
    # Auto mode scales with payload and member count.
    assert choose_workers(0, 10_000, 512 * 1024 * 1024) >= 2
    # No files at all still yields a valid worker count.
    assert choose_workers(0, 0, 0) == 1
