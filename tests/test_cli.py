"""Smoke tests for the command line interface."""

from __future__ import annotations

import zipfile
from pathlib import Path

from unzipfast.cli import main


def make_zip(path: Path, files: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return path


def test_cli_extracts_into_archive_stem(tmp_path: Path, monkeypatch, capsys) -> None:
    archive = make_zip(tmp_path / "photos.zip", {"a.txt": b"a"})
    monkeypatch.chdir(tmp_path)

    code = main([str(archive), "-q"])

    assert code == 0
    assert (tmp_path / "photos" / "a.txt").read_bytes() == b"a"
    assert capsys.readouterr().out == ""


def test_cli_respects_output_and_include(tmp_path: Path, capsys) -> None:
    archive = make_zip(
        tmp_path / "data.zip",
        {"keep.csv": b"1", "drop.txt": b"2"},
    )
    destination = tmp_path / "out"

    code = main(
        [str(archive), "-o", str(destination), "-i", "*.csv", "--no-progress"]
    )

    assert code == 0
    assert (destination / "keep.csv").exists()
    assert not (destination / "drop.txt").exists()
    output = capsys.readouterr().out
    assert "Extracted 1 file(s)" in output


def test_cli_list_mode(tmp_path: Path, capsys) -> None:
    archive = make_zip(tmp_path / "data.zip", {"dir/a.txt": b"hello"})

    code = main([str(archive), "-l"])

    assert code == 0
    output = capsys.readouterr().out
    assert "dir/a.txt" in output
    assert "1 entries" in output
    assert "5 B" in output


def test_cli_test_mode(tmp_path: Path, capsys) -> None:
    archive = make_zip(tmp_path / "data.zip", {"a.txt": b"hello"})

    code = main([str(archive), "-t"])

    assert code == 0
    assert "OK: 1 entries verified" in capsys.readouterr().out


def test_cli_missing_archive(tmp_path: Path, capsys) -> None:
    code = main([str(tmp_path / "nope.zip")])

    assert code == 1
    assert "archive not found" in capsys.readouterr().err


def test_cli_invalid_archive(tmp_path: Path, capsys) -> None:
    bogus = tmp_path / "bogus.zip"
    bogus.write_text("this is not a zip file")

    code = main([str(bogus)])

    assert code == 1
    assert "cannot open" in capsys.readouterr().err


def test_cli_reports_skipped_entries(tmp_path: Path, capsys) -> None:
    archive = make_zip(tmp_path / "evil.zip", {"../escape.txt": b"x", "ok.txt": b"ok"})
    destination = tmp_path / "out"

    code = main([str(archive), "-o", str(destination), "--no-progress"])

    assert code == 0
    output = capsys.readouterr().out
    assert "Skipped 1 entry" in output
    assert "unsafe path" in output
    assert (destination / "ok.txt").exists()


def test_cli_max_size_refuses_large_archive(tmp_path: Path, capsys) -> None:
    archive = make_zip(tmp_path / "big.zip", {"a.bin": b"\0" * 4096})
    destination = tmp_path / "out"

    code = main([str(archive), "-o", str(destination), "--max-size", "1K"])

    assert code == 1
    assert "--max-size" in capsys.readouterr().err
    assert not destination.exists()


def test_cli_max_size_allows_small_archive(tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "ok.zip", {"a.txt": b"hello"})
    destination = tmp_path / "out"

    code = main([str(archive), "-o", str(destination), "--max-size", "1M", "-q"])

    assert code == 0
    assert (destination / "a.txt").read_bytes() == b"hello"


def test_parse_size() -> None:
    from unzipfast.cli import parse_size

    assert parse_size("500") == 500
    assert parse_size("1K") == 1024
    assert parse_size("2g") == 2 << 30
    assert parse_size("1.5MiB") == int(1.5 * (1 << 20))
