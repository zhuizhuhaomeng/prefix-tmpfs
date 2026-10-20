# prefix-tmpfs

Run a command with selected directory-name prefixes backed by tmpfs. Other
paths read and write the original filesystem, including newly created files
and directories. This is a FUSE filesystem with explicit routing, rather than
a copy-on-write OverlayFS layer.

## Usage

```sh
./prefix-tmpfs --source /path/to/tests \
    --prefix cache_ --prefix tmp_ -- command args
```

For example, `cache_work/` and `tmp_123/` directly below the source directory
use tmpfs. `data/`, `config.txt`, and `data/cache_nested/` stay on the original
filesystem. The command continues to see the original source pathname.

Alternatively, configure comma-separated prefixes in the environment:

```sh
PREFIX_TMPFS_PREFIXES='cache_,tmp_' \
    ./prefix-tmpfs --source /path/to/tests -- command args
```

The source directory and at least one prefix are required. Repeated `--prefix`
options replace the environment setting. There are no application-specific
defaults. Use `--tmpdir /path/on/tmpfs` to select a backing parent other than
`/tmp`. Use `--help` to display the options.

## Routing rules

- Prefixes match literal names of directories immediately below the source.
  They are not glob patterns or regular expressions.
- All selected directories share one tmpfs backend. Renames and hard links
  between their contents work normally.
- Matching on-disk directories are hidden in the command's view and preserved.
  Their contents are not copied into memory.
- Regular files and symlinks whose names match a prefix stay on disk. A symlink
  is resolved normally and can lead outside the selected directories.
- Empty prefixes, `.` and `..`, `/`, embedded NULs, and prefixes longer than
  255 filesystem bytes are rejected.
- Spaces around environment entries are trimmed. Use `--prefix` to preserve
  literal leading/trailing spaces or commas.
- Moves and hard links between the memory and disk backends return `EXDEV`.
- Unix socket endpoints must be created and accessed through the same view.

## Isolation and cleanup

The source directory is covered only in the command's private mount namespace.
Other terminals and editors retain the original view. Each invocation gets
an independent backing directory, even when invocations share a source.

The launcher stops the command's PID namespace, unmounts the FUSE view and
stops the filesystem daemon. Successful commands remove their temporary data.
Failed or interrupted commands retain the backing directory and print its
location to stderr. Startup failures also retain the directory and daemon log.
Inspect `memory/` for files still present when the command stopped and
`fuse.log` for filesystem diagnostics, then remove that exact retained directory
when it is no longer needed. Subsequent invocations use fresh directories.

Retention cannot recover files that the wrapped program deleted itself. For
example, Test::Nginx's normal non-randomized mode leaves its server root for
inspection, while randomized mode removes its server root on exit.

If unmounting fails, the launcher preserves the daemon and temporary directory
rather than traversing the live passthrough mount during cleanup. A forced
`SIGKILL` of the launcher cannot run its cleanup handler.

This reduces writes to the original filesystem, but FUSE adds userspace
round trips and can be slower for small files. Tmpfs may use swap if the
system enables it; this tool does not change the system's swap policy.

## Performance

The launcher defaults to a single FUSE request thread. Parallel small-file
workloads can otherwise spend more CPU scheduling libfuse workers and
contending on their shared request queue than performing tmpfs operations.
Wrapped commands still run in parallel; only filesystem request handling is
serialized. This limits the daemon to approximately one CPU core, but does
not limit the CPU used by the wrapped commands or the rest of the kernel.
Requests involving slow or blocking backing files can hold up other requests
in this mode. For those workloads, select the dynamic worker pool with
`--multi-thread` or `PREFIX_TMPFS_SINGLE_THREAD=0`:

```sh
PREFIX_TMPFS_SINGLE_THREAD=0 python3 prefix_tmpfs.py \
    --source /path/to/tests --prefix cache_ -- command args
```

`--single-thread` and `--multi-thread` override the environment. The environment
accepts only `0` or `1`, and defaults to `1`. Python callers can select the pool
with `single_thread=False` in `PrefixMount` or `run`.

By default, positive lookups and attributes are cached for one second to
reduce repeated path lookups for small files. Ordinary directories are
streamed; only the merged root needs a snapshot.

Disable the cache when edits, renames and deletions made through the original
source must remain immediately visible:

```sh
PREFIX_TMPFS_CACHE_TIMEOUT=0 ./prefix-tmpfs \
    --source /path/to/tests --prefix cache_ -- command args
```

`--cache-timeout SECONDS` overrides the environment variable. The default is
`1` second; values must be finite and non-negative, and fractions are accepted.
This setting applies to positive directory lookups and file attributes on
both backends. Missing names are never cached. Writes and namespace changes
through the mounted view update the kernel normally. Changes through another
view of the original source may remain stale until the timeout expires;
use `0` when immediate visibility is required. This is metadata caching,
not writeback caching: writes still reach the selected backend directly.

Measure on the filesystem used by your project:

```sh
make bench
# Compare single-thread and multi-thread CPU costs under parallel load:
python3 benchmarks/small_files.py --workers 16 --cycles 100 --samples 3
# Optional comparison with a previously built helper:
python3 benchmarks/small_files.py --baseline-binary /path/to/old/prefix-tmpfs
```

The benchmark creates, writes, reads and recursively removes directories of
small files. It rotates backend order and reports medians and individual
samples for disk, native tmpfs and FUSE with/without caching. Build, mount and
namespace setup time are excluded; these are not end-to-end application times.
Fixtures use `build/benchmarks/` on the project filesystem and `/tmp` by default.
The benchmark reports daemon CPU seconds and average CPU utilization alongside
elapsed time. `--workers` starts independent client processes, and `--cycles`
is the number of directories per worker. CPU measurements cover the daemon,
not the client processes; CPU tick resolution can affect very short samples.
Parallel runs use separate creation/readback and deletion phases. Serial runs
retain the mixed create/read/delete workload. Samples use fresh directory
names so old dentries do not carry over between rounds.

On Linux 5.14 / libfuse 3.10.2, the 16-worker command above measured these
medians with other tests running on the same host:

| Request handling (cache = 1 second) | Elapsed seconds | Daemon CPU seconds | Average daemon CPU |
| --- | ---: | ---: | ---: |
| Single thread | 1.4754 | 1.450 | 98% |
| Dynamic worker pool | 3.8602 | 11.150 | 289% |

These numbers describe this workload, not a general throughput guarantee.
The mixed parallel create/read/delete stress probe also exposed an unresolved
cleanup race in the dynamic worker pool: `.fuse_hidden` files sometimes remain
and cause `rmdir` to return `ENOTEMPTY`. The phased benchmark avoids overlapping
close and unlink; it does not demonstrate that this cleanup race is fixed.

An earlier local run using the dynamic worker pool on Linux 5.14 / libfuse
3.10.2, with one client, 200 directories, five 1 KiB files per directory and
five samples, measured:

| Backend | Median seconds |
| --- | ---: |
| Original filesystem | 0.1382 |
| Native tmpfs | 0.1010 |
| FUSE, cache disabled | 0.9822 |
| FUSE, 1-second metadata cache | 0.5914 |

Caching reduced this workload's FUSE time by about 40%, but FUSE remained
substantially slower than native tmpfs. Reducing disk writes and reducing
elapsed time are separate goals. When the application can directly choose a
temporary root on tmpfs, using that path avoids FUSE's per-operation round trips.

## Requirements and build

- Linux with working user namespaces and access to `/dev/fuse`.
- FUSE 3 runtime, `fusermount3` and development headers (`fuse3-devel` on
  Rocky Linux, `libfuse3-dev` on Debian).
- Bubblewrap (`bwrap`), a C compiler, `pkg-config` and Python 3.9 or later.

No third-party Python packages are needed. System dependencies are never
installed automatically. The launcher builds the native helper on demand
at `build/prefix-tmpfs`, or build it explicitly:

```sh
make
```

`CC` selects the compiler. `PKG_CONFIG_PATH` can select another FUSE 3
development installation. Build artifacts and test fixtures stay in `build/`.

## Tests

```sh
make test
```

The tests cover configuration validation, literal and overlapping prefixes,
disk write-through, hidden old directories, rename/link behavior, open files
after unlink, metadata, mmap, Unix sockets, parallel operations, namespace
isolation, cancellation, daemon crashes and failed unmounts. They also cover
uncached external changes, cached in-view mutations and large streamed listings.

Filesystem tests skip when `/dev/fuse` is unavailable; configuration tests
still run. Actual mount tests require the dependencies listed above.
