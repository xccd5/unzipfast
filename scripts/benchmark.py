#!/usr/bin/env python3
"""Micro-benchmark: serial vs parallel extraction.

Usage:
    python scripts/benchmark.py                 # 2000 files x 4 KiB
    python scripts/benchmark.py -n 5000 -s 8192 # 5000 files x 8 KiB
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from unzipfast.archive import build_plan  # noqa: E402
from unzipfast.extractor import extract_archive  # noqa: E402
from unzipfast.progress import human_size  # noqa: E402


def create_archive(path: Path, files: int, size: int) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        for index in range(files):
            zf.writestr(f"dataset/{index % 64:02d}/sample_{index:05d}.bin", os.urandom(size))


def time_run(archive: Path, destination: Path, jobs: int) -> float:
    if destination.exists():
        shutil.rmtree(destination)
    plan = build_plan(archive, destination)
    started = time.perf_counter()
    result = extract_archive(plan, archive, jobs=jobs)
    elapsed = time.perf_counter() - started
    if result.errors:
        raise SystemExit(f"extraction failed: {result.errors[:3]}")
    return elapsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-n", "--files", type=int, default=2000)
    parser.add_argument("-s", "--size", type=int, default=4096, help="bytes per file")
    parser.add_argument("-r", "--rounds", type=int, default=2, help="rounds per mode")
    args = parser.parse_args()

    cpus = os.cpu_count() or 1
    print(
        f"platform: {platform.system()} {platform.release()} / "
        f"Python {platform.python_version()} / {cpus} CPUs"
    )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        archive = root / "benchmark.zip"
        print(f"creating {args.files:,} files x {human_size(args.size)} ...")
        create_archive(archive, args.files, args.size)
        payload = args.files * args.size
        print(f"archive: {human_size(archive.stat().st_size)} compressed, "
              f"{human_size(payload)} uncompressed")
        print()

        serial = min(
            time_run(archive, root / "serial", 1) for _ in range(args.rounds)
        )
        parallel = min(
            time_run(archive, root / "parallel", cpus) for _ in range(args.rounds)
        )

    def report(label: str, elapsed: float) -> None:
        print(f"{label:<22} {elapsed:6.2f} s   {human_size(payload / elapsed):>10}/s")

    report("serial (1 worker)", serial)
    report(f"parallel ({cpus} workers)", parallel)
    print(f"{'speedup':<22} {serial / parallel:6.2f}x")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
