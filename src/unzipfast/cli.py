"""Command line interface for unzipfast."""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from . import __version__
from .archive import Plan, Skip, build_plan, open_archive, read_entries, resolve_password
from .errors import ArchiveError, UnzipfastError
from .extractor import Result, choose_workers, extract_archive, verify_archive
from .progress import Progress, human_duration, human_size

EPILOG = """\
examples:
  unzipfast photos.zip                    extract into ./photos/
  unzipfast photos.zip -o D:/photos       extract into D:/photos
  unzipfast big.zip -j 8                  use 8 worker processes
  unzipfast data.zip -i "*.csv" -x old\\*
  unzipfast secure.zip -p hunter2         ZipCrypto or AES password
  unzipfast photos.zip -l                 list contents, extract nothing
  unzipfast photos.zip -t                 verify the CRC of every member

environment:
  UNZIPFAST_PASSWORD   fallback password when -p is not given
  UNZIPFAST_JOBS       default worker count (0 = one per CPU)
"""


_SIZE_UNITS = {"": 1, "k": 1 << 10, "m": 1 << 20, "g": 1 << 30, "t": 1 << 40}


def parse_size(text: str) -> int:
    """Parse a size such as ``500M`` or ``2G`` (binary units) into bytes."""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kmgt]?)i?b?\s*", text, re.IGNORECASE)
    if not match:
        raise argparse.ArgumentTypeError(f"invalid size: {text!r} (examples: 500M, 2G)")
    return int(float(match.group(1)) * _SIZE_UNITS[match.group(2).lower()])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="unzipfast",
        description="A fast, safe, parallel ZIP extractor.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("archive", nargs="?", help="path to the .zip archive")
    parser.add_argument(
        "-o", "--output", metavar="DIR", help="destination directory (default: <archive name>)"
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=_env_int("UNZIPFAST_JOBS", 0),
        metavar="N",
        help="worker processes (0 = auto, the default)",
    )
    parser.add_argument(
        "-p",
        "--password",
        action="append",
        default=[],
        metavar="PWD",
        help="archive password; repeat to try several",
    )
    parser.add_argument(
        "-i",
        "--include",
        action="append",
        default=[],
        metavar="GLOB",
        help='only extract matching members, e.g. "*.csv" (repeatable)',
    )
    parser.add_argument(
        "-x",
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help='skip matching members, e.g. "node_modules/*" (repeatable)',
    )
    parser.add_argument(
        "-f", "--flat", action="store_true", help="ignore directory structure"
    )
    parser.add_argument(
        "--overwrite",
        choices=("skip", "overwrite", "rename"),
        default="skip",
        help="what to do with existing files (default: skip)",
    )
    parser.add_argument(
        "-l", "--list", dest="list_mode", action="store_true", help="list contents and exit"
    )
    parser.add_argument(
        "-t", "--test", action="store_true", help="verify every member's CRC and exit"
    )
    parser.add_argument(
        "--max-size",
        type=parse_size,
        metavar="SIZE",
        help="refuse to extract more than SIZE in total, e.g. 2G (guards against zip bombs)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="show every skipped entry")
    parser.add_argument("-q", "--quiet", action="store_true", help="only print errors")
    parser.add_argument("--no-progress", action="store_true", help="disable the progress bar")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [
        max([len(header)] + [len(row[index]) for row in rows])
        for index, header in enumerate(headers[:-1])
    ]
    template = "  ".join(f"{{:>{width}}}" for width in widths) + "  {}"
    print(template.format(*headers))
    print("  ".join("-" * width for width in widths) + "  " + "-" * len(headers[-1]))
    for row in rows:
        print(template.format(*row))


def _print_skipped(skipped: list[Skip], verbose: bool) -> None:
    if not skipped:
        return
    label = "entry" if len(skipped) == 1 else "entries"
    print(f"Skipped {len(skipped):,} {label}:")
    shown = skipped if verbose else skipped[:5]
    for item in shown:
        print(f"  - {item.name} ({item.reason})")
    if len(skipped) > len(shown):
        print(f"  ... and {len(skipped) - len(shown):,} more (use -v to show all)")


def _list(archive: Path) -> int:
    with open_archive(archive) as zf:
        entries = read_entries(zf)

    if not entries:
        print(f"{archive}: empty archive")
        return 0

    total_size = sum(entry.size for entry in entries)
    total_packed = sum(entry.compressed_size for entry in entries)
    rows: list[list[str]] = []
    for entry in entries:
        ratio = f"{entry.compressed_size / entry.size * 100:.0f}%" if entry.size else "-"
        modified = "%04d-%02d-%02d %02d:%02d" % entry.date_time[:5]
        name = entry.name + ("/" if entry.is_dir else "")
        if entry.encrypted:
            name += "  [encrypted]"
        if entry.is_symlink:
            name += "  [symlink]"
        rows.append(
            [human_size(entry.size), human_size(entry.compressed_size), ratio, modified, name]
        )

    _print_table(["Size", "Packed", "Ratio", "Modified", "Name"], rows)
    print()
    ratio = f"{total_packed / total_size * 100:.0f}%" if total_size else "-"
    print(
        f"{len(entries):,} entries, {human_size(total_size)} uncompressed, "
        f"{human_size(total_packed)} compressed ({ratio} of original)"
    )
    return 0


def _test(archive: Path, args: argparse.Namespace, passwords: list[str]) -> int:
    with open_archive(archive) as zf:
        password = resolve_password(zf, passwords)
        infos = [info for info in zf.infolist() if not info.is_dir()]
        total = sum(info.file_size for info in infos)

    progress = Progress(
        total,
        len(infos),
        enabled=not (args.quiet or args.no_progress),
    )
    checked, failures = verify_archive(archive, password, progress)

    if failures:
        total_entries = checked + len(failures)
        print(
            f"FAILED: {len(failures):,} of {total_entries:,} entries are corrupted "
            "or unreadable",
            file=sys.stderr,
        )
        for name, error in failures:
            print(f"  - {name}: {error}", file=sys.stderr)
        return 1

    print(f"OK: {checked:,} entries verified ({human_size(total)})")
    return 0


def _extract(
    archive: Path, args: argparse.Namespace, passwords: list[str]
) -> int:
    output = Path(args.output) if args.output else Path(archive.stem)
    plan: Plan = build_plan(
        archive,
        output,
        include=args.include or (),
        exclude=args.exclude or (),
        flat=args.flat,
        overwrite=args.overwrite,
    )

    if args.max_size is not None and plan.total_bytes > args.max_size:
        raise ArchiveError(
            f"archive would extract {human_size(plan.total_bytes)}, which is more than "
            f"--max-size {human_size(args.max_size)}"
        )

    if not args.quiet:
        print(f"Archive: {archive}")
        print(f"Output:  {output}")
        if plan.encrypted:
            print("Encrypted: yes")
        print(f"Files:   {len(plan.tasks):,} ({human_size(plan.total_bytes)})")
        workers = choose_workers(args.jobs, len(plan.tasks), plan.total_bytes)
        if workers > 1:
            print(f"Workers: {workers}")

    if not plan.tasks and not plan.directories:
        if not args.quiet:
            _print_skipped(plan.skipped, args.verbose)
            print("Nothing to extract.")
        return 0

    output.mkdir(parents=True, exist_ok=True)
    workers = choose_workers(args.jobs, len(plan.tasks), plan.total_bytes)
    progress = Progress(
        plan.total_bytes,
        len(plan.tasks),
        enabled=not (args.quiet or args.no_progress),
    )
    result: Result = extract_archive(
        plan, archive, jobs=workers, passwords=passwords, progress=progress
    )

    if not args.quiet:
        speed = result.bytes / result.elapsed if result.elapsed > 0 else 0.0
        print(
            f"Extracted {result.files:,} file(s), {human_size(result.bytes)} in "
            f"{human_duration(result.elapsed)} ({human_size(speed)}/s)"
        )
        _print_skipped(plan.skipped, args.verbose)

    if result.errors:
        label = "entry" if len(result.errors) == 1 else "entries"
        print(f"{len(result.errors):,} {label} failed:", file=sys.stderr)
        for target, error in result.errors[:20]:
            print(f"  - {target}: {error}", file=sys.stderr)
        if len(result.errors) > 20:
            print(f"  ... and {len(result.errors) - 20:,} more", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.archive:
        parser.error("the archive argument is required")
    if args.list_mode and args.test:
        parser.error("--list and --test cannot be combined")

    archive = Path(args.archive)
    passwords = list(args.password or [])
    env_password = os.environ.get("UNZIPFAST_PASSWORD")
    if env_password:
        passwords.append(env_password)

    try:
        if args.list_mode:
            return _list(archive)
        if args.test:
            return _test(archive, args, passwords)
        return _extract(archive, args, passwords)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except UnzipfastError as exc:
        print(f"unzipfast: error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"unzipfast: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
