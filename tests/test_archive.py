"""Tests for archive metadata handling and extraction planning."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from unzipfast.archive import build_plan, normalize_member, read_entries


def make_zip(path: Path, files: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return path


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("a.txt", ("a.txt",)),
        ("dir/sub/file.txt", ("dir", "sub", "file.txt")),
        ("dir//./file.txt", ("dir", "file.txt")),
        ("dir\\file.txt", ("dir", "file.txt")),
        ("/absolute/file.txt", ("absolute", "file.txt")),
        ("", None),
        (".", None),
        ("../evil.txt", None),
        ("dir/../../evil.txt", None),
        ("C:/Windows/evil.txt", None),
        ("bad\x00name", None),
    ],
)
def test_normalize_member(name: str, expected: tuple[str, ...] | None) -> None:
    assert normalize_member(name) == expected


def test_plan_extracts_safe_members_only(tmp_path: Path) -> None:
    archive = make_zip(
        tmp_path / "evil.zip",
        {"../escape.txt": b"x", "good.txt": b"ok", "nested/also.txt": b"ok"},
    )
    destination = tmp_path / "out"

    plan = build_plan(archive, destination)

    targets = {Path(task.target) for task in plan.tasks}
    assert targets == {destination / "good.txt", destination / "nested" / "also.txt"}
    assert any(skip.reason == "unsafe path" for skip in plan.skipped)
    assert not (tmp_path / "escape.txt").exists()


def test_plan_include_and_exclude(tmp_path: Path) -> None:
    archive = make_zip(
        tmp_path / "data.zip",
        {
            "keep.csv": b"a",
            "keep.txt": b"b",
            "skip.csv": b"c",
            "nested/keep.csv": b"d",
        },
    )
    plan = build_plan(
        archive,
        tmp_path / "out",
        include=["*.csv"],
        exclude=["skip*"],
    )
    names = {Path(task.target).name for task in plan.tasks}
    assert names == {"keep.csv"}


def test_plan_flat_mode_renames_collisions(tmp_path: Path) -> None:
    archive = make_zip(
        tmp_path / "flat.zip",
        {"one/same.txt": b"1", "two/same.txt": b"2"},
    )
    plan = build_plan(archive, tmp_path / "out", flat=True, overwrite="rename")
    names = sorted(Path(task.target).name for task in plan.tasks)
    assert names == ["same (1).txt", "same.txt"]


def test_plan_skips_existing_files(tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "a.zip", {"a.txt": b"new"})
    destination = tmp_path / "out"
    destination.mkdir()
    (destination / "a.txt").write_bytes(b"old")

    plan = build_plan(archive, destination, overwrite="skip")
    assert plan.tasks == []
    assert [skip.reason for skip in plan.skipped] == ["already exists"]


def test_plan_renames_existing_files(tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "a.zip", {"a.txt": b"new"})
    destination = tmp_path / "out"
    destination.mkdir()
    (destination / "a.txt").write_bytes(b"old")

    plan = build_plan(archive, destination, overwrite="rename")
    assert [Path(task.target).name for task in plan.tasks] == ["a (1).txt"]


def test_plan_collects_directory_entries(tmp_path: Path) -> None:
    archive = tmp_path / "dirs.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("empty/", b"")
        zf.writestr("full/file.txt", b"data")

    plan = build_plan(archive, tmp_path / "out")
    assert [Path(path).name for path in plan.directories] == ["empty"]
    assert [Path(task.target).name for task in plan.tasks] == ["file.txt"]


def test_plan_skips_symlinks(tmp_path: Path) -> None:
    archive = tmp_path / "link.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        info = zipfile.ZipInfo("link")
        info.external_attr = (0o120777 << 16)
        zf.writestr(info, "target")
        zf.writestr("real.txt", b"real")

    plan = build_plan(archive, tmp_path / "out")
    assert [Path(task.target).name for task in plan.tasks] == ["real.txt"]
    assert any("symlink" in skip.reason for skip in plan.skipped)


def test_plan_rejects_file_as_destination(tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "a.zip", {"a.txt": b"a"})
    destination = tmp_path / "file"
    destination.write_text("not a directory")

    with pytest.raises(Exception, match="not a directory"):
        build_plan(archive, destination)


def test_read_entries_reports_metadata(tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "meta.zip", {"a.txt": b"hello"})
    with zipfile.ZipFile(archive) as zf:
        entries = read_entries(zf)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.name == "a.txt"
    assert entry.size == 5
    assert entry.is_dir is False
    assert entry.encrypted is False
    assert len(entry.date_time) == 6


def test_plan_skips_existing_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    destination = tmp_path / "out"
    destination.mkdir()
    try:
        (destination / "link").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available on this system")
    archive = make_zip(tmp_path / "a.zip", {"link/pwned.txt": b"x", "ok.txt": b"ok"})

    plan = build_plan(archive, destination, overwrite="overwrite")

    assert {Path(task.target) for task in plan.tasks} == {destination / "ok.txt"}
    assert any(skip.reason == "outside destination" for skip in plan.skipped)
