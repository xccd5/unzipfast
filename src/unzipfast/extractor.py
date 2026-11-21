"""The extraction engine.

Each worker process opens its own handle to the archive (a single ``ZipFile``
cannot be shared between processes because the underlying file offset is
shared after ``fork``), decompresses the members it is given and writes them
through a temporary file so a crash never leaves a half-written file behind.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from .archive import Plan, Task, open_archive, resolve_password
from .errors import UnzipfastError
from .progress import Progress

CHUNK_SIZE = 1024 * 1024

_WorkerState: dict[str, object] = {}
_Handle = Callable[[Task, int, "str | None"], None]


@dataclass
class Result:
    """Summary of an extraction run."""

    files: int = 0
    bytes: int = 0
    elapsed: float = 0.0
    workers: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)


def _worker_init(archive_path: str, password: bytes | None) -> None:
    _WorkerState["zip"] = open_archive(archive_path, password)
    _WorkerState["infos"] = _WorkerState["zip"].infolist()


def _worker_extract(task: Task) -> tuple[Task, int, str | None]:
    zf = _WorkerState["zip"]
    infos = _WorkerState["infos"]
    try:
        written = _write_member(zf, infos[task.index], Path(task.target))
        return task, written, None
    except Exception as exc:  # reported per file, the run continues
        return task, 0, f"{type(exc).__name__}: {exc}"


def _write_member(zf, info, target: Path) -> int:
    """Decompress one member into ``target`` atomically."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".unzipfast-{os.getpid()}-{uuid.uuid4().hex[:8]}.part")
    written = 0
    try:
        with zf.open(info) as source, open(temp, "wb") as sink:
            while True:
                chunk = source.read(CHUNK_SIZE)
                if not chunk:
                    break
                sink.write(chunk)
                written += len(chunk)
        _apply_metadata(temp, info)
        os.replace(temp, target)
    except BaseException:
        try:
            temp.unlink()
        except OSError:
            pass
        raise
    return written


def _apply_metadata(path: Path, info) -> None:
    try:
        # zipfile stores local time without timezone information.
        mtime = time.mktime(tuple(info.date_time) + (0, 0, -1))
        os.utime(path, (mtime, mtime))
    except (OverflowError, OSError, ValueError):
        pass
    mode = (info.external_attr >> 16) & 0o777
    if mode and os.name != "nt":
        try:
            os.chmod(path, mode)
        except OSError:
            pass


def normalize_jobs(jobs: int, files: int) -> int:
    """Clamp the requested worker count to something sensible."""
    if jobs is None or jobs <= 0:
        jobs = os.cpu_count() or 1
    return max(1, min(int(jobs), max(files, 1)))


def choose_workers(jobs: int, files: int, total_bytes: int) -> int:
    """Pick a worker count.

    Spawning processes is not free (~30-50 ms each), so tiny archives are
    extracted serially even when ``jobs`` is 0 ("auto").  Roughly one worker
    per 8 MiB of payload and per 64 members is a good balance.
    """
    if jobs is None or jobs <= 0:
        cpus = os.cpu_count() or 1
        by_size = max(1, total_bytes // (8 << 20))
        by_files = max(1, files // 64)
        jobs = min(cpus, by_size, by_files)
    return normalize_jobs(jobs, files)


def _extract_serial(
    archive_path: str,
    password: bytes | None,
    tasks: Sequence[Task],
    handle: _Handle,
) -> None:
    zf = open_archive(archive_path, password)
    try:
        infos = zf.infolist()
        for task in tasks:
            try:
                written = _write_member(zf, infos[task.index], Path(task.target))
                error = None
            except Exception as exc:
                written, error = 0, f"{type(exc).__name__}: {exc}"
            handle(task, written, error)
    finally:
        zf.close()


def _extract_parallel(
    archive_path: str,
    password: bytes | None,
    tasks: Sequence[Task],
    jobs: int,
    handle: _Handle,
) -> None:
    context = mp.get_context("spawn")
    chunksize = max(1, min(64, len(tasks) // (jobs * 4) or 1))
    try:
        with context.Pool(
            processes=jobs, initializer=_worker_init, initargs=(archive_path, password)
        ) as pool:
            for task, written, error in pool.imap_unordered(
                _worker_extract, tasks, chunksize=chunksize
            ):
                handle(task, written, error)
    except Exception as exc:
        raise UnzipfastError(f"extraction workers failed: {exc}") from exc


def extract_archive(
    plan: Plan,
    archive_path: str | os.PathLike[str],
    *,
    jobs: int = 0,
    passwords: Sequence[str] = (),
    progress: Progress | None = None,
) -> Result:
    """Extract every task in ``plan`` and return a summary."""
    result = Result()
    started = time.perf_counter()

    for directory in plan.directories:
        try:
            Path(directory).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            result.errors.append((directory, f"{type(exc).__name__}: {exc}"))

    if plan.tasks:
        archive = os.fspath(archive_path)
        with open_archive(archive) as zf:
            password = resolve_password(zf, passwords)

        workers = choose_workers(jobs, len(plan.tasks), plan.total_bytes)
        result.workers = workers

        def handle(task: Task, written: int, error: str | None) -> None:
            if error is not None:
                result.errors.append((task.target, error))
            else:
                result.files += 1
                result.bytes += written
            if progress is not None:
                progress.advance(written if error is None else task.size, 1)

        if workers == 1:
            _extract_serial(archive, password, plan.tasks, handle)
        else:
            _extract_parallel(archive, password, plan.tasks, workers, handle)

    result.elapsed = time.perf_counter() - started
    if progress is not None:
        progress.finish()
    return result


def verify_archive(
    archive_path: str | os.PathLike[str],
    password: bytes | None,
    progress: Progress | None = None,
) -> tuple[int, list[tuple[str, str]]]:
    """Read every member and let ``zipfile`` verify its CRC.

    Returns ``(checked, failures)`` where ``failures`` holds
    ``(member_name, error_message)`` pairs.
    """
    checked = 0
    failures: list[tuple[str, str]] = []
    with open_archive(archive_path, password) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            try:
                with zf.open(info) as handle:
                    while handle.read(CHUNK_SIZE):
                        pass
                checked += 1
            except Exception as exc:
                failures.append((info.filename, f"{type(exc).__name__}: {exc}"))
            if progress is not None:
                progress.advance(info.file_size, 1)
    return checked, failures
