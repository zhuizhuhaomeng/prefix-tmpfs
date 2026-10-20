#!/usr/bin/env python3
"""Compare small-file churn on disk, tmpfs and prefix-tmpfs (mount time excluded)."""

import argparse
import concurrent.futures
import contextlib
import os
from pathlib import Path
import shutil
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import prefix_tmpfs as fs


def workload(base, cycles, files, worker=0, generation=0, phase="all"):
    data = b"x" * 1024
    start = time.perf_counter()
    for index in range(cycles):
        root = base / f"cache_{generation}_{worker}_{index}"
        if phase != "remove":
            root.mkdir()
            for number in range(files):
                path = root / str(number)
                path.write_bytes(data)
                if path.read_bytes() != data:
                    raise RuntimeError("read did not match written data")
        if phase != "create":
            shutil.rmtree(root)
    return time.perf_counter() - start


def daemon_cpu(mount):
    if mount is None:
        return 0
    fields = Path(f"/proc/{mount.daemon.pid}/stat").read_text().rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


def run_workload(pool, base, cycles, files, workers, generation=0):
    if pool is None:
        workload(base, cycles, files, generation=generation)
    else:
        # Separate creation/readback and deletion to measure both without
        # racing libfuse's asynchronous releases against unlink/hidden-file
        # handling. This is a phased workload, not mixed deletion churn.
        for phase in ("create", "remove"):
            futures = [pool.submit(workload, base, cycles, files, worker, generation, phase)
                       for worker in range(workers)]
            for future in futures:
                future.result()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycles", type=int, default=200)
    parser.add_argument("--files", type=int, default=5)
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--workers", type=int, default=1,
                        help="independent processes; cycles are per worker")
    parser.add_argument("--tmpdir", type=Path, default=Path("/tmp"))
    parser.add_argument("--baseline-binary", type=Path,
                        help="optionally compare an older native helper")
    args = parser.parse_args()
    if min(args.cycles, args.files, args.samples, args.workers) < 1:
        parser.error("cycles, files, samples and workers must be positive")
    binary = fs.build_binary()
    fixtures = fs.ROOT / "build/benchmarks"
    fixtures.mkdir(parents=True, exist_ok=True)
    mounts = []
    try:
        with tempfile.TemporaryDirectory(dir=fixtures) as directory:
            source = Path(directory).resolve()
            with tempfile.TemporaryDirectory(dir=args.tmpdir) as directory:
                cases = [("disk", source, None), ("tmpfs", Path(directory), None)]
                configurations = [("FUSE single cache=0", binary, 0, True),
                                  ("FUSE single cache=1s", binary, 1, True),
                                  ("FUSE multi cache=1s", binary, 1, False)]
                if args.baseline_binary:
                    configurations.insert(0, ("FUSE previous", args.baseline_binary.resolve(),
                                              0, False))
                for label, helper, timeout, single_thread in configurations:
                    mount = fs.PrefixMount(source, args.tmpdir, helper, ["cache_"],
                                           cache_timeout=timeout, single_thread=single_thread)
                    mounts.append(mount)
                    mount.start()
                    cases.append((label, mount.view, mount))
                samples = {label: [] for label, _, _ in cases}
                cpu_samples = {label: [] for label, _, _ in cases}
                with contextlib.ExitStack() as stack:
                    pool = None
                    if args.workers > 1:
                        pool = stack.enter_context(concurrent.futures.ProcessPoolExecutor(
                            max_workers=args.workers))
                    for _, base, _ in cases:
                        run_workload(pool, base, min(10, args.cycles), args.files, args.workers)
                    # Rotate the order to reduce warmup/order bias.
                    for index in range(args.samples):
                        ordered = cases[index % len(cases):] + cases[:index % len(cases)]
                        for label, base, mount in ordered:
                            before = daemon_cpu(mount)
                            start = time.perf_counter()
                            # Fresh names keep later samples independent of
                            # cached dentries left by earlier samples.
                            run_workload(pool, base, args.cycles, args.files, args.workers,
                                         generation=index + 1)
                            samples[label].append(time.perf_counter() - start)
                            cpu_samples[label].append(daemon_cpu(mount) - before)
                disk = statistics.median(samples["disk"])
                print(f"{args.workers} workers, {args.cycles} directories/worker/sample, "
                      f"{args.files} files/directory, "
                      f"1 KiB/file, {args.samples} samples; mount/build excluded")
                print(f"{'backend':<23} {'median (s)':>10} {'vs disk':>9} "
                      f"{'daemon CPU (s)':>14} {'avg CPU':>9}  samples (s)")
                for label, _, mount in cases:
                    median = statistics.median(samples[label])
                    values = ", ".join(f"{value:.4f}" for value in samples[label])
                    used_cpu = statistics.median(cpu_samples[label])
                    average = statistics.median(cpu / wall * 100 for cpu, wall
                                                in zip(cpu_samples[label], samples[label]))
                    cpu_text = f"{used_cpu:.3f}" if mount else "-"
                    average_text = f"{average:.0f}%" if mount else "-"
                    print(f"{label:<23} {median:10.4f} {median / disk:8.2f}x "
                          f"{cpu_text:>14} {average_text:>9}  {values}")
    finally:
        for mount in reversed(mounts):
            mount.close()


if __name__ == "__main__":
    main()
