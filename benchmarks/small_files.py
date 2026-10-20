#!/usr/bin/env python3
"""Compare small-file churn on disk, tmpfs and prefix-tmpfs (mount time excluded)."""

import argparse
from pathlib import Path
import shutil
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import prefix_tmpfs as fs


def workload(base, cycles, files):
    data = b"x" * 1024
    start = time.perf_counter()
    for index in range(cycles):
        root = base / ("cache_" + str(index))
        root.mkdir()
        for number in range(files):
            path = root / str(number)
            path.write_bytes(data)
            if path.read_bytes() != data:
                raise RuntimeError("read did not match written data")
        shutil.rmtree(root)
    return time.perf_counter() - start


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycles", type=int, default=200)
    parser.add_argument("--files", type=int, default=5)
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--tmpdir", type=Path, default=Path("/tmp"))
    parser.add_argument("--baseline-binary", type=Path,
                        help="optionally compare an older native helper")
    args = parser.parse_args()
    if min(args.cycles, args.files, args.samples) < 1:
        parser.error("cycles, files and samples must be positive")
    binary = fs.build_binary()
    fixtures = fs.ROOT / "build/benchmarks"
    fixtures.mkdir(parents=True, exist_ok=True)
    mounts = []
    try:
        with tempfile.TemporaryDirectory(dir=fixtures) as directory:
            source = Path(directory).resolve()
            with tempfile.TemporaryDirectory(dir=args.tmpdir) as directory:
                cases = [("disk", source), ("tmpfs", Path(directory))]
                configurations = [("FUSE cache=0", binary, 0),
                                  ("FUSE cache=1s", binary, 1)]
                if args.baseline_binary:
                    configurations.insert(0, ("FUSE previous", args.baseline_binary.resolve(), 0))
                for label, helper, timeout in configurations:
                    mount = fs.PrefixMount(source, args.tmpdir, helper, ["cache_"],
                                           cache_timeout=timeout)
                    mounts.append(mount)
                    mount.start()
                    cases.append((label, mount.view))
                samples = {label: [] for label, _ in cases}
                for _, base in cases:
                    workload(base, min(10, args.cycles), args.files)
                # Rotate the order to reduce warmup/order bias.
                for index in range(args.samples):
                    ordered = cases[index % len(cases):] + cases[:index % len(cases)]
                    for label, base in ordered:
                        samples[label].append(workload(base, args.cycles, args.files))
                disk = statistics.median(samples["disk"])
                print(f"{args.cycles} directories/sample, {args.files} files/directory, "
                      f"1 KiB/file, {args.samples} samples; mount/build excluded")
                print(f"{'backend':<18} {'median (s)':>12} {'vs disk':>10}  samples (s)")
                for label, _ in cases:
                    median = statistics.median(samples[label])
                    values = ", ".join(f"{value:.4f}" for value in samples[label])
                    print(f"{label:<18} {median:12.4f} {median / disk:9.2f}x  {values}")
    finally:
        for mount in reversed(mounts):
            mount.close()


if __name__ == "__main__":
    main()
