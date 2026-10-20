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
removes temporary data on completion, failure or interruption. If unmounting
fails, it preserves the daemon and temporary directory rather than traversing
the live passthrough mount during cleanup. A forced `SIGKILL` of the launcher
cannot run its cleanup handler.

This reduces writes to the original filesystem, but FUSE adds userspace
round trips and can be slower for small files. Tmpfs may use swap if the
system enables it; this tool does not change the system's swap policy.

## Performance

By default, positive lookups and attributes are not cached: edits, renames
and deletions made through the original source remain immediately visible.
This is expensive for workloads that repeatedly resolve paths to small files.
Ordinary directories are streamed; only the merged root needs a snapshot.

For a test run whose source files are stable, enable a short metadata cache:

```sh
PREFIX_TMPFS_CACHE_TIMEOUT=1 ./prefix-tmpfs \
    --source /path/to/tests --prefix cache_ -- command args
```

`--cache-timeout SECONDS` overrides the environment variable. The default is
`0`; values must be finite and non-negative, and fractions are accepted.
This setting applies to positive directory lookups and file attributes on
both backends. Missing names are never cached. Writes and namespace changes
through the mounted view update the kernel normally. Changes through another
view of the original source may remain stale until the timeout expires;
use `0` when immediate visibility is required. This is metadata caching,
not writeback caching: writes still reach the selected backend directly.

Measure on the filesystem used by your project:

```sh
make bench
# Optional comparison with a previously built helper:
python3 benchmarks/small_files.py --baseline-binary /path/to/old/prefix-tmpfs
```

The benchmark creates, writes, reads and recursively removes directories of
small files. It rotates backend order and reports medians and individual
samples for disk, native tmpfs and FUSE with/without caching. Build, mount and
namespace setup time are excluded; these are not end-to-end application times.
Fixtures use `build/benchmarks/` on the project filesystem and `/tmp` by default.

One local run on Linux 5.14 / libfuse 3.10.2, with 200 directories, five 1 KiB
files per directory and five samples, measured:

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
