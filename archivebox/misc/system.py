__package__ = "archivebox.misc"


import os
import signal
import sys

from json import dump
from pathlib import Path
from subprocess import PIPE, Popen, CalledProcessError, CompletedProcess, TimeoutExpired

from atomicwrites import atomic_write as lib_atomic_write

from archivebox.config.common import get_config
from archivebox.misc.util import enforce_types, ExtendedEncoder

IS_WINDOWS = os.name == "nt"


def run(cmd, *args, input=None, capture_output=True, timeout=None, check=False, text=False, start_new_session=True, **kwargs):
    """Patched of subprocess.run to kill forked child subprocesses and fix blocking io making timeout=innefective
    Mostly copied from https://github.com/python/cpython/blob/master/Lib/subprocess.py
    """

    cmd = [str(arg) for arg in cmd]

    if input is not None:
        if kwargs.get("stdin") is not None:
            raise ValueError("stdin and input arguments may not both be used.")
        kwargs["stdin"] = PIPE

    if capture_output:
        if ("stdout" in kwargs) or ("stderr" in kwargs):
            raise ValueError("stdout and stderr arguments may not be used with capture_output.")
        kwargs["stdout"] = PIPE
        kwargs["stderr"] = PIPE

    pgid = None
    try:
        if isinstance(cmd, (list, tuple)) and cmd[0].endswith(".py"):
            PYTHON_BINARY = sys.executable
            cmd = (PYTHON_BINARY, *cmd)

        with Popen(cmd, *args, start_new_session=start_new_session, text=text, **kwargs) as process:
            pgid = os.getpgid(process.pid)
            try:
                stdout, stderr = process.communicate(input, timeout=timeout)
            except TimeoutExpired as exc:
                process.kill()
                if IS_WINDOWS:
                    # Windows accumulates the output in a single blocking
                    # read() call run on child threads, with the timeout
                    # being done in a join() on those threads.  communicate()
                    # _after_ kill() is required to collect that and add it
                    # to the exception.
                    timed_out_stdout, timed_out_stderr = process.communicate()
                    exc.stdout = timed_out_stdout.encode() if isinstance(timed_out_stdout, str) else timed_out_stdout
                    exc.stderr = timed_out_stderr.encode() if isinstance(timed_out_stderr, str) else timed_out_stderr
                else:
                    # POSIX _communicate already populated the output so
                    # far into the TimeoutExpired exception.
                    process.wait()
                raise
            except BaseException:  # Including KeyboardInterrupt, communicate handled that.
                process.kill()
                # We don't call process.wait() as .__exit__ does that for us.
                raise

            retcode = process.poll()
            if check and retcode:
                raise CalledProcessError(
                    retcode,
                    process.args,
                    output=stdout,
                    stderr=stderr,
                )
    finally:
        # force kill any straggler subprocesses that were forked from the main proc
        try:
            if pgid is not None:
                os.killpg(pgid, signal.SIGINT)
        except Exception:
            pass

    return CompletedProcess(process.args, retcode or 0, stdout, stderr)


@enforce_types
def atomic_write(path: Path | str, contents: dict | str | bytes, overwrite: bool = True, config=None, **config_kwargs) -> None:
    """Safe atomic write to filesystem by writing to temp file + atomic rename"""

    mode = "wb+" if isinstance(contents, bytes) else "w"
    encoding = None if isinstance(contents, bytes) else "utf-8"  # enforce utf-8 on all text writes

    # print('\n> Atomic Write:', mode, path, len(contents), f'overwrite={overwrite}')
    try:
        with lib_atomic_write(path, mode=mode, overwrite=overwrite, encoding=encoding) as f:
            if isinstance(contents, dict):
                dump(contents, f, indent=4, sort_keys=True, cls=ExtendedEncoder)
            elif isinstance(contents, (bytes, str)):
                f.write(contents)
    except OSError as e:
        config = config or get_config(**config_kwargs)
        if config.ENFORCE_ATOMIC_WRITES:
            print(f"[X] OSError: Failed to write {path} with fcntl.F_FULLFSYNC. ({e})")
            print(
                "    You can store the archive/ subfolder on a hard drive or network share that doesn't support support synchronous writes,",
            )
            print(
                "    but the main folder containing the index.sqlite3 and ArchiveBox.conf files must be on a filesystem that supports FSYNC.",
            )
            raise SystemExit(1)

        # retry the write without forcing FSYNC (aka atomic mode)
        with open(path, mode=mode, encoding=encoding) as f:
            if isinstance(contents, dict):
                dump(contents, f, indent=4, sort_keys=True, cls=ExtendedEncoder)
            elif isinstance(contents, (bytes, str)):
                f.write(contents)

    # set file permissions
    config = config or get_config(**config_kwargs)
    os.chmod(path, int(config.OUTPUT_PERMISSIONS, base=8))


@enforce_types
def get_dir_size(path: str | Path, recursive: bool = True, pattern: str | None = None) -> tuple[int, int, int]:
    """get the total disk size of a given directory, optionally summing up
    recursively and limiting to a given filter list
    """
    num_bytes, num_dirs, num_files = 0, 0, 0
    try:
        for entry in os.scandir(path):
            if (pattern is not None) and (pattern not in entry.path):
                continue
            if entry.is_dir(follow_symlinks=False):
                if not recursive:
                    continue
                num_dirs += 1
                bytes_inside, dirs_inside, files_inside = get_dir_size(entry.path)
                num_bytes += bytes_inside
                num_dirs += dirs_inside
                num_files += files_inside
            else:
                num_bytes += entry.stat(follow_symlinks=False).st_size
                num_files += 1
    except OSError:
        # e.g. FileNameTooLong or other error while trying to read dir
        pass
    return num_bytes, num_dirs, num_files
