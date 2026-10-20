#!/usr/bin/env python3
"""Run a command with selected directory prefixes backed by tmpfs."""

import argparse
import math
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parent


def validate_cache_timeout(value):
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        raise ValueError("cache timeout must be a finite, non-negative number") from None
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("cache timeout must be a finite, non-negative number")
    return timeout


def validate_prefixes(prefixes=None):
    if prefixes is None:
        raise ValueError("at least one directory-name prefix is required")
    if isinstance(prefixes, str):
        raise ValueError("prefixes must be a list of directory-name prefixes")
    result = []
    for prefix in prefixes:
        if (not isinstance(prefix, str) or not prefix or "/" in prefix
                or "\0" in prefix or prefix in (".", "..")
                or len(os.fsencode(prefix)) > 255):
            raise ValueError("invalid directory-name prefix: " + repr(prefix))
        if prefix not in result:
            result.append(prefix)
    if not result:
        raise ValueError("at least one directory-name prefix is required")
    return tuple(result)


def infrastructure_env():
    env = os.environ.copy()
    # Apply instrumentation only to the wrapped command, not the filesystem
    # daemon, compiler, mount helper, or namespace launcher.
    env.pop("LD_PRELOAD", None)
    return env


def build_binary():
    source = ROOT / "src/prefix-tmpfs.c"
    binary = ROOT / "build/prefix-tmpfs"
    if binary.is_file() and binary.stat().st_mtime_ns >= source.stat().st_mtime_ns:
        return binary

    env = infrastructure_env()
    flags = subprocess.run(["pkg-config", "--cflags", "--libs", "fuse3"],
                           env=env, capture_output=True, text=True)
    if flags.returncode:
        raise RuntimeError("prefix-tmpfs requires the FUSE 3 development headers "
                           "(fuse3-devel or libfuse3-dev) and pkg-config")

    binary.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="prefix-tmpfs-", dir=binary.parent)
    os.close(fd)
    try:
        command = [*shlex.split(env.get("CC", "cc")), "-O2", "-g", "-Wall",
                   "-Wextra", "-Werror", "-D_FILE_OFFSET_BITS=64", str(source),
                   "-o", temporary, *shlex.split(flags.stdout)]
        subprocess.run(command, env=env, check=True)
        os.chmod(temporary, 0o755)
        os.replace(temporary, binary)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return binary


def is_mounted(path):
    # stat() is unreliable after a FUSE daemon crashes (ENOTCONN).
    escaped = str(path).replace("\\", "\\134").replace(" ", "\\040")
    escaped = escaped.replace("\t", "\\011").replace("\n", "\\012")
    return any(line.split()[4] == escaped
               for line in Path("/proc/self/mountinfo").read_text().splitlines())


def stop(process, group=False):
    if process is None or process.poll() is not None:
        return
    try:
        if group:
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=5)
    except ProcessLookupError:
        process.wait()
    except subprocess.TimeoutExpired:
        if group:
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait()


class PrefixMount:
    def __init__(self, source, parent, binary, prefixes=None, cache_timeout=0):
        self.prefixes = validate_prefixes(prefixes)
        self.cache_timeout = validate_cache_timeout(cache_timeout)
        self.source = Path(source).resolve(strict=True)
        parent = Path(parent).resolve(strict=True)
        if not self.source.is_dir() or not parent.is_dir():
            raise RuntimeError("source and tmpfs parent must be directories")
        if self.source == parent or self.source in parent.parents:
            raise RuntimeError("tmpfs parent must be outside the source directory")
        kind = subprocess.check_output(["stat", "-f", "-c", "%T", str(parent)],
                                       env=infrastructure_env(), text=True).strip()
        if kind != "tmpfs":
            raise RuntimeError("backing parent must be on tmpfs: " + str(parent))
        self.base = Path(tempfile.mkdtemp(prefix="prefix-tmpfs-", dir=parent))
        self.memory = self.base / "memory"
        self.view = self.base / "view"
        self.log = self.base / "fuse.log"
        self.binary = binary
        self.daemon = None
        try:
            self.memory.mkdir()
            self.view.mkdir()
        except OSError:
            shutil.rmtree(self.base)
            raise

    def start(self):
        options = [value for prefix in self.prefixes for value in ("--prefix", prefix)]
        if self.cache_timeout:
            options.extend(("--cache-timeout", str(self.cache_timeout)))
        with self.log.open("wb") as output:
            self.daemon = subprocess.Popen(
                [str(self.binary), *options, str(self.source),
                 str(self.memory), str(self.view),
                 "-f", "-o", "default_permissions,fsname=prefix-tmpfs"],
                stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                env=infrastructure_env(), start_new_session=True)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.daemon.poll() is not None:
                raise RuntimeError("prefix-tmpfs mount failed: "
                                   + self.log.read_text(errors="replace").strip())
            if is_mounted(self.view):
                return
            time.sleep(0.05)
        raise RuntimeError("timed out mounting prefix-tmpfs: " + str(self.view))

    def close(self):
        if is_mounted(self.view):
            for option in ("-u", "-uz"):
                try:
                    subprocess.run(["fusermount3", option, str(self.view)],
                                   env=infrastructure_env(), capture_output=True,
                                   timeout=5)
                except subprocess.TimeoutExpired:
                    continue
                if not is_mounted(self.view):
                    break
        if is_mounted(self.view):
            # Never recurse through a still-mounted passthrough view: that
            # would delete the caller's original files.
            raise RuntimeError("cannot unmount " + str(self.view)
                               + "; retained daemon and temporary directory")
        stop(self.daemon)
        if self.base.exists():
            shutil.rmtree(self.base)


def run(source, parent, command, preload=None, prefixes=None, cache_timeout=0):
    prefixes = validate_prefixes(prefixes)
    for tool in ("bwrap", "fusermount3", "pkg-config"):
        if shutil.which(tool) is None:
            raise RuntimeError("prefix-tmpfs requires " + tool)
    if not Path("/dev/fuse").exists():
        raise RuntimeError("prefix-tmpfs requires access to /dev/fuse")

    mount = PrefixMount(source, parent, build_binary(), prefixes, cache_timeout)
    child = None
    try:
        mount.start()
        patterns = ", ".join(prefix + "*" for prefix in prefixes)
        print("prefix-tmpfs: " + str(mount.source) + "/ [" + patterns
              + "] -> " + str(mount.memory), flush=True)
        env = infrastructure_env()
        command = ["bwrap", "--die-with-parent", "--unshare-pid", "--bind", "/", "/",
                   "--dev-bind", "/dev", "/dev", "--proc", "/proc",
                   "--bind", str(mount.view), str(mount.source),
                   "--chdir", str(Path.cwd()), "--setenv", "LD_PRELOAD",
                   preload if preload is not None else os.environ.get("LD_PRELOAD", ""),
                   "--", *command]
        child = subprocess.Popen(command, env=env, start_new_session=True)
        status = child.wait()
        return status if status >= 0 else 128 - status
    finally:
        try:
            stop(child, group=True)
        finally:
            mount.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--tmpdir", type=Path, default="/tmp")
    parser.add_argument("--prefix", action="append", metavar="NAME",
                        help="directory-name prefix; repeat for multiple prefixes "
                             "(or use PREFIX_TMPFS_PREFIXES)")
    parser.add_argument("--preload")
    parser.add_argument("--cache-timeout", type=validate_cache_timeout,
                        default=os.environ.get("PREFIX_TMPFS_CACHE_TIMEOUT", "0"),
                        metavar="SECONDS",
                        help="cache positive lookups and attributes for this many "
                             "seconds (default: PREFIX_TMPFS_CACHE_TIMEOUT or 0)")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("a command is required after --")
    prefixes = args.prefix
    if prefixes is None and "PREFIX_TMPFS_PREFIXES" in os.environ:
        prefixes = [value.strip() for value in os.environ["PREFIX_TMPFS_PREFIXES"].split(",")]
    try:
        prefixes = validate_prefixes(prefixes)
    except ValueError as error:
        parser.error(str(error))

    def interrupted(signum, _frame):
        # Finish teardown even if the terminal sends another interrupt.
        for handled in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(handled, signal.SIG_IGN)
        raise InterruptedError(signum)

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, interrupted)

    try:
        return run(args.source, args.tmpdir, command, args.preload, prefixes,
                   args.cache_timeout)
    except InterruptedError as error:
        return 128 + error.args[0]
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print("prefix-tmpfs: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
