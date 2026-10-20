#!/usr/bin/env python3
"""Compare small append writes with an optional older native FUSE helper."""

import argparse
import concurrent.futures
from pathlib import Path
import os
import statistics
import tempfile
import time

from small_files import daemon_cpu
import prefix_tmpfs as fs


def append_log(base, worker, generation, writes):
    root = base / f"cache_{generation}_{worker}"
    root.mkdir()
    path = root / "error.log"
    line = b"debug: prefix-tmpfs append benchmark\n"
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    try:
        for _ in range(writes):
            if os.write(fd, line) != len(line):
                raise RuntimeError("short append write")
        if os.fstat(fd).st_size != len(line) * writes:
            raise RuntimeError("appended file size did not match")
    finally:
        os.close(fd)


def run(pool, base, workers, generation, writes):
    jobs = [pool.submit(append_log, base, worker, generation, writes)
            for worker in range(workers)]
    for job in jobs:
        job.result()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--writes", type=int, default=10000, help="writes per worker/sample")
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--threads", type=fs.parse_threads, default=fs.DEFAULT_THREADS)
    parser.add_argument("--baseline-binary", type=Path)
    parser.add_argument("--tmpdir", type=Path, default=Path("/tmp"))
    args = parser.parse_args()
    if min(args.workers, args.writes, args.samples) < 1:
        parser.error("workers, writes and samples must be positive")
    fixtures = fs.ROOT / "build/benchmarks"
    fixtures.mkdir(parents=True, exist_ok=True)
    mounts = []
    try:
        with tempfile.TemporaryDirectory(dir=fixtures) as source, \
                tempfile.TemporaryDirectory(dir=args.tmpdir) as memory:
            cases = [("native tmpfs", Path(memory), None)]
            helpers = [("FUSE current", fs.build_binary())]
            if args.baseline_binary:
                helpers.append(("FUSE baseline", args.baseline_binary.resolve()))
            for label, helper in helpers:
                mount = fs.PrefixMount(source, args.tmpdir, helper, ["cache_"],
                                       threads=args.threads)
                mounts.append(mount)
                mount.start()
                cases.append((label, mount.view, mount))
            timings = {label: [] for label, _, _ in cases}
            cpu_times = {label: [] for label, _, _ in cases}
            with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers) as pool:
                for _, base, _ in cases:
                    run(pool, base, args.workers, -1, min(100, args.writes))
                for sample in range(args.samples):
                    ordered = cases[sample % len(cases):] + cases[:sample % len(cases)]
                    for label, base, mount in ordered:
                        before = daemon_cpu(mount)
                        start = time.perf_counter()
                        run(pool, base, args.workers, sample, args.writes)
                        timings[label].append(time.perf_counter() - start)
                        cpu_times[label].append(daemon_cpu(mount) - before)
            print(f"{args.workers} clients, {args.writes} writes/client/sample, "
                  f"{args.samples} samples, {args.threads} FUSE workers")
            print("mount/build/cleanup excluded; CPU covers the FUSE daemon only")
            print(f"{'backend':<18} {'wall median (s)':>16} {'CPU median (s)':>16}  wall samples")
            for label, _, mount in cases:
                wall = statistics.median(timings[label])
                cpu = f"{statistics.median(cpu_times[label]):.3f}" if mount else "-"
                samples = ", ".join(f"{value:.4f}" for value in timings[label])
                print(f"{label:<18} {wall:16.4f} {cpu:>16}  {samples}")
    finally:
        for mount in reversed(mounts):
            mount.close()


if __name__ == "__main__":
    main()
