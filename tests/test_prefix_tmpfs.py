"""Integration checks for the prefix-routed test filesystem (Linux/FUSE 3)."""

import concurrent.futures
import contextlib
import errno
import importlib.util
import io
import mmap
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parent.parent / "prefix_tmpfs.py"
SPEC = importlib.util.spec_from_file_location("prefix_tmpfs", SCRIPT.with_name("prefix_tmpfs.py"))
fs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fs)


def churn_files(base, worker):
    for cycle in range(30):
        root = base / f"sandbox_{worker}_{cycle}"
        root.mkdir()
        for number in range(4):
            path = root / str(number)
            path.write_bytes(b"x" * 1024)
            if path.read_bytes() != b"x" * 1024:
                raise AssertionError("parallel readback mismatch")
        shutil.rmtree(root)


class PrefixConfiguration(unittest.TestCase):
    def test_cli_threading_configuration(self):
        cases = ((None, [], 4), ("1", [], 1), ("0", [], 0),
                 ("0", ["--single-thread"], 1),
                 ("1", ["--multi-thread"], 0),
                 ("invalid", ["--single-thread"], 1))
        for setting, options, expected in cases:
            with self.subTest(setting=setting, options=options), \
                    patch.dict(os.environ), \
                    patch.object(sys, "argv", [str(SCRIPT), "--source", "/tmp",
                                               "--prefix", "tmp_", *options,
                                               "--", "true"]), \
                    patch.object(fs.signal, "signal"), \
                    patch.object(fs, "run", return_value=7) as run:
                os.environ.pop("PREFIX_TMPFS_THREADS", None)
                if setting is None:
                    os.environ.pop("PREFIX_TMPFS_SINGLE_THREAD", None)
                else:
                    os.environ["PREFIX_TMPFS_SINGLE_THREAD"] = setting
                self.assertEqual(fs.main(), 7)
                self.assertEqual(run.call_args.kwargs["threads"], expected)

    def test_worker_count_configuration_and_precedence(self):
        cases = (({}, [], 4), ({"PREFIX_TMPFS_THREADS": "2"}, [], 2),
                 ({"PREFIX_TMPFS_THREADS": "64"}, [], 64),
                 ({"PREFIX_TMPFS_THREADS": "0"}, [], 0),
                 ({"PREFIX_TMPFS_THREADS": "2", "PREFIX_TMPFS_SINGLE_THREAD": "1"}, [], 2),
                 ({"PREFIX_TMPFS_THREADS": "bad"}, ["--threads", "8"], 8),
                 ({"PREFIX_TMPFS_THREADS": "bad"}, ["--single-thread"], 1),
                 ({"PREFIX_TMPFS_THREADS": "bad"}, ["--multi-thread"], 0))
        for settings, options, expected in cases:
            with self.subTest(settings=settings, options=options), \
                    patch.dict(os.environ), \
                    patch.object(sys, "argv", [str(SCRIPT), "--source", "/tmp", "--prefix",
                                               "tmp_", *options, "--", "true"]), \
                    patch.object(fs.signal, "signal"), \
                    patch.object(fs, "run", return_value=7) as run:
                os.environ.pop("PREFIX_TMPFS_THREADS", None)
                os.environ.pop("PREFIX_TMPFS_SINGLE_THREAD", None)
                os.environ.update(settings)
                self.assertEqual(fs.main(), 7)
                self.assertEqual(run.call_args.kwargs["threads"], expected)

    def test_worker_count_validation(self):
        for value in (None, -1, 65, "", "bad", "1.5", 1.5, True, "9" * 100):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "threads must be"):
                    fs.validate_threads(value)
        self.assertEqual(fs.resolve_threads(), 4)
        self.assertEqual(fs.resolve_threads(single_thread=True), 1)
        self.assertEqual(fs.resolve_threads(single_thread=False), 0)
        with self.assertRaisesRegex(ValueError, "cannot both"):
            fs.resolve_threads(threads=4, single_thread=True)

    def test_cli_rejects_invalid_worker_counts(self):
        for value in ("-1", "65", "bad", "1.5", ""):
            for options, settings in ((["--threads", value], {}),
                                      ([], {"PREFIX_TMPFS_THREADS": value})):
                with self.subTest(value=value, options=options):
                    result = subprocess.run(
                        [sys.executable, str(SCRIPT), "--source", "/tmp", "--prefix", "tmp_",
                         *options, "--", "true"], env={**os.environ, **settings},
                        text=True, capture_output=True, timeout=5)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn("threads must be an integer", result.stderr)

    def test_invalid_threading_configuration_does_not_run_command(self):
        for value in ("", "2", "false", "bad"):
            with self.subTest(value=value):
                result = subprocess.run(
                    [sys.executable, str(SCRIPT), "--source", "/tmp", "--prefix", "tmp_",
                    "--", "true"], env={name: setting for name, setting in
                                        {**os.environ, "PREFIX_TMPFS_SINGLE_THREAD": value}.items()
                                        if name != "PREFIX_TMPFS_THREADS"},
                    text=True, capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("PREFIX_TMPFS_SINGLE_THREAD must be 0 or 1", result.stderr)

    def test_cache_timeout_validation(self):
        for value, expected in ((0, 0), ("0", 0), ("0.25", 0.25), (1, 1)):
            self.assertEqual(fs.validate_cache_timeout(value), expected)
        for value in (None, "", "bad", -1, "-0.1", "nan", "inf", "-inf"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "cache timeout"):
                    fs.validate_cache_timeout(value)

    def test_cli_cache_timeout_configuration(self):
        for setting in ("-1", "nan", "inf", "bad", ""):
            env = {**os.environ, "PREFIX_TMPFS_CACHE_TIMEOUT": setting}
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--source", "/tmp", "--prefix", "tmp_",
                 "--", "true"], env=env, text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("--cache-timeout", result.stderr)

        cases = ((None, [], 1), ("0", [], 0), ("0.25", [], 0.25),
                 ("0.25", ["--cache-timeout", "1"], 1),
                 ("1", ["--cache-timeout", "0"], 0))
        for setting, options, expected in cases:
            arguments = [str(SCRIPT), "--source", "/tmp", "--prefix", "tmp_",
                         *options, "--", "true"]
            with patch.dict(os.environ), \
                    patch.object(sys, "argv", arguments), \
                    patch.object(fs.signal, "signal"), \
                    patch.object(fs, "run", return_value=7) as run:
                if setting is None:
                    os.environ.pop("PREFIX_TMPFS_CACHE_TIMEOUT", None)
                else:
                    os.environ["PREFIX_TMPFS_CACHE_TIMEOUT"] = setting
                self.assertEqual(fs.main(), 7)
                self.assertEqual(run.call_args.args[-1], expected)

    def test_source_and_prefixes_are_required(self):
        env = os.environ.copy()
        env.pop("PREFIX_TMPFS_PREFIXES", None)
        with tempfile.TemporaryDirectory() as directory:
            for options, message in (([], "--source"),
                                     (["--source", directory],
                                      "at least one directory-name prefix")):
                result = subprocess.run([sys.executable, str(SCRIPT), *options,
                                         "--", "true"], env=env, text=True,
                                        capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(message, result.stderr)

    def test_explicit_and_duplicate_prefixes(self):
        self.assertEqual(fs.validate_prefixes(["sandbox"]), ("sandbox",))
        self.assertEqual(fs.validate_prefixes(["cache_", "tmp_", "cache_"]),
                         ("cache_", "tmp_"))

    def test_invalid_prefixes_are_rejected(self):
        for prefixes in (None, [], [""], ["."], [".."], ["cache/"], ["a/b"],
                         ["x\0y"], ["x" * 256], ["缓" * 86], [None], "cache"):
            with self.subTest(prefixes=prefixes):
                with self.assertRaises(ValueError):
                    fs.validate_prefixes(prefixes)

    def test_cli_rejects_bad_configuration_before_running_command(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "executed"
            command = [sys.executable, "-c",
                       "from pathlib import Path; Path(__import__('sys').argv[1]).touch()",
                       str(marker)]
            for value in ("", "cache,,tmp", "cache,", ".", "a/b"):
                with self.subTest(value=value):
                    env = os.environ.copy()
                    env["PREFIX_TMPFS_PREFIXES"] = value
                    result = subprocess.run([sys.executable, str(SCRIPT),
                                             "--source", directory, "--", *command],
                                            env=env, text=True, capture_output=True,
                                            timeout=5)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn("directory-name prefix", result.stderr)
                    self.assertFalse(marker.exists())


@unittest.skipUnless(Path("/dev/fuse").exists(), "requires access to /dev/fuse")
class PrefixFilesystem(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.binary = fs.build_binary()

    def setUp(self):
        # Source fixtures must be on the repository's filesystem, rather than
        # /tmp, so a routing bug cannot accidentally pass the backing checks.
        fixtures = fs.ROOT / "build/test-fixtures"
        fixtures.mkdir(parents=True, exist_ok=True)
        fixture = tempfile.TemporaryDirectory(prefix="prefix-tmpfs-test-", dir=fixtures)
        self.addCleanup(fixture.cleanup)
        self.source = Path(fixture.name) / "t"
        self.source.mkdir()
        self.mount = fs.PrefixMount(self.source, "/tmp", self.binary, ["sandbox"])
        self.addCleanup(self.mount.close)
        self.mount.start()
        self.view = self.mount.view
        self.memory = self.mount.memory

    def test_ordinary_files_and_new_directories_write_through(self):
        mount = self.custom_mount(["sandbox"], cache_timeout=0)
        path = mount.view / "data/sandbox_nested/file"
        path.parent.mkdir(parents=True)
        path.write_text("first")
        original = self.source / "data/sandbox_nested/file"
        self.assertEqual(original.read_text(), "first")
        original.write_text("outside edit")
        self.assertEqual(path.read_text(), "outside edit")
        path.write_text("changed")
        renamed = path.with_name("renamed")
        path.rename(renamed)
        self.assertEqual(original.with_name("renamed").read_text(), "changed")
        renamed.unlink()
        self.assertFalse(original.with_name("renamed").exists())
        self.assertEqual(list(mount.memory.iterdir()), [])

    def test_uncached_external_metadata_and_name_changes_are_immediate(self):
        mount = self.custom_mount(["sandbox"], cache_timeout=0)
        path = mount.view / "ordinary"
        original = self.source / path.name
        self.assertFalse(path.exists())
        original.write_bytes(b"a")
        self.assertEqual(path.stat().st_size, 1)
        original.write_bytes(b"longer")
        self.assertEqual(path.stat().st_size, 6)
        original.chmod(0o600)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        original.rename(self.source / "renamed")
        self.assertFalse(path.exists())
        self.assertEqual((mount.view / "renamed").read_bytes(), b"longer")
        (self.source / "renamed").unlink()
        self.assertFalse((mount.view / "renamed").exists())

    def test_cached_metadata_and_in_view_mutations(self):
        mount = fs.PrefixMount(self.source, "/tmp", self.binary, ["sandbox"],
                               cache_timeout=60)
        self.addCleanup(mount.close)
        mount.start()
        path = mount.view / "ordinary"
        path.write_bytes(b"a")
        self.assertEqual(path.stat().st_size, 1)
        (self.source / path.name).write_bytes(b"external edit")
        # Positive attributes are cached, but missing names are never cached.
        self.assertEqual(path.stat().st_size, 1)
        missing = mount.view / "external-new"
        self.assertFalse(missing.exists())
        (self.source / missing.name).write_text("new")
        self.assertEqual(missing.read_text(), "new")
        path.write_bytes(b"updated")
        self.assertEqual(path.stat().st_size, 7)
        path.chmod(0o640)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
        for _ in range(3):
            root = mount.view / "sandbox_cache"
            root.mkdir()
            file = root / "file"
            file.write_bytes(b"contents")
            self.assertEqual(file.read_bytes(), b"contents")
            renamed = root / "renamed"
            file.rename(renamed)
            self.assertFalse(file.exists())
            self.assertEqual(renamed.read_bytes(), b"contents")
            os.link(renamed, root / "link")
            self.assertEqual((root / "link").read_bytes(), b"contents")
            shutil.rmtree(root)
            self.assertFalse(root.exists())
            self.assertFalse((mount.memory / root.name).exists())

    def test_all_sandbox_prefixes_use_memory(self):
        for name in ("sandbox", "sandbox_1234", "sandbox_extra"):
            root = self.view / name
            root.mkdir()
            (root / "file").write_text(name)
            self.assertEqual((self.memory / name / "file").read_text(), name)
            self.assertFalse((self.source / name).exists())
        (self.view / "not_sandbox").mkdir()
        self.assertTrue((self.source / "not_sandbox").is_dir())

    def custom_mount(self, prefixes, **options):
        mount = fs.PrefixMount(self.source, "/tmp", self.binary, prefixes, **options)
        self.addCleanup(mount.close)
        mount.start()
        return mount

    def test_multiple_prefixes_share_the_memory_backend(self):
        mount = self.custom_mount(["sandbox", "cache_", "scratch."])
        old = self.source / "cache_old"
        old.mkdir()
        (old / "keep").write_text("original")
        self.assertFalse((mount.view / "cache_old").exists())
        self.assertNotIn("cache_old", os.listdir(mount.view))
        for name in ("sandbox_1", "cache_old", "scratch.work"):
            root = mount.view / name
            root.mkdir()
            (root / "file").write_text(name)
            self.assertEqual((mount.memory / name / "file").read_text(), name)
        os.link(mount.view / "sandbox_1/file", mount.view / "cache_old/link")
        (mount.view / "sandbox_1/file").rename(mount.view / "scratch.work/moved")
        self.assertEqual((mount.view / "cache_old/link").read_text(), "sandbox_1")
        (mount.view / "scratch.work").rename(mount.view / "cache_renamed")
        self.assertEqual(set(os.listdir(mount.view)),
                         {"sandbox_1", "cache_old", "cache_renamed"})
        for name in os.listdir(mount.view):
            shutil.rmtree(mount.view / name)
        self.assertEqual((old / "keep").read_text(), "original")
        self.assertEqual(list(mount.memory.iterdir()), [])
        (mount.view / "cache_old").mkdir()
        self.assertTrue((mount.memory / "cache_old").is_dir())

    def test_custom_prefixes_replace_default_and_match_literally(self):
        prefixes = ["cache[1].", "cache dir", "缓存_", "scratch", "scratch_",
                    "literal,", " spaced"]
        mount = self.custom_mount(prefixes)
        for prefix in prefixes:
            name = prefix + "test"
            (mount.view / name).mkdir()
            (mount.view / name / "file").write_text(name)
            self.assertEqual((mount.memory / name / "file").read_text(), name)
            self.assertFalse((self.source / name).exists())
        for name in ("sandbox_1", "cache1.test", "cache", "data/cache[1].nested"):
            (mount.view / name).mkdir(parents=True)
            (mount.view / name / "file").write_text("disk")
            self.assertEqual((self.source / name / "file").read_text(), "disk")
        (mount.view / "cache[1].regular").write_text("regular file")
        self.assertEqual((self.source / "cache[1].regular").read_text(), "regular file")

    def test_cli_prefixes_override_environment_and_environment_supports_multiple(self):
        code = '''from pathlib import Path
import sys
root = Path(sys.argv[1])
for name in ('cache_run', 'scratch_run', 'sandbox_run'):
    (root / name).mkdir()
    (root / name / 'file').write_text(name)
'''
        cases = (([], "cache_, scratch_", {"cache_run", "scratch_run"}),
                 (["--prefix", "sandbox"], "cache_,scratch_", {"sandbox_run"}),
                 (["--prefix", "cache_", "--prefix", "scratch_"], "sandbox",
                  {"cache_run", "scratch_run"}),
                 (["--prefix", "sandbox"], "", {"sandbox_run"}))
        for options, setting, selected in cases:
            for name in ("cache_run", "scratch_run", "sandbox_run"):
                shutil.rmtree(self.source / name, ignore_errors=True)
            env = os.environ.copy()
            env["PREFIX_TMPFS_PREFIXES"] = setting
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--source", str(self.source),
                 *options, "--", sys.executable, "-c", code, str(self.source)],
                env=env, text=True, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for name in ("cache_run", "scratch_run", "sandbox_run"):
                self.assertEqual((self.source / name).exists(), name not in selected)
            base = Path(result.stdout.strip().split(" -> ")[-1]).parent
            self.assertFalse(base.exists())

    def test_native_helper_rejects_invalid_prefixes(self):
        for prefix in ("", ".", "..", "a/b", "x" * 256):
            with self.subTest(prefix=prefix):
                result = subprocess.run(
                    [str(self.binary), "--prefix", prefix, str(self.source),
                     str(self.memory), str(self.mount.base / "unused")],
                    text=True, capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("invalid directory-name prefix", result.stderr)

    def test_native_helper_requires_prefixes(self):
        result = subprocess.run(
            [str(self.binary), str(self.source), str(self.memory),
             str(self.mount.base / "unused")], text=True, capture_output=True,
            timeout=5)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("at least one --prefix", result.stderr)

    def test_native_helper_rejects_invalid_cache_timeout(self):
        for value in ("-1", "nan", "inf", "bad", "", "1x"):
            result = subprocess.run(
                [str(self.binary), "--cache-timeout", value, "--prefix", "sandbox",
                 str(self.source), str(self.memory), str(self.mount.base / "unused")],
                text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("invalid cache timeout", result.stderr)

    def test_sandbox_prefixed_files_and_symlinks_stay_on_disk(self):
        path = self.view / "sandbox-file"
        path.write_text("disk file")
        path.rename(self.view / "sandbox-renamed")
        os.link(self.view / "sandbox-renamed", self.view / "sandbox-link")
        os.symlink("sandbox-renamed", self.view / "sandbox-symlink")
        self.assertEqual((self.source / "sandbox-link").read_text(), "disk file")
        self.assertEqual((self.view / "sandbox-symlink").read_text(), "disk file")
        self.assertEqual(set(os.listdir(self.view)),
                         {"sandbox-renamed", "sandbox-link", "sandbox-symlink"})
        with (self.view / "sandbox-renamed").open("r+b") as output:
            (self.view / "sandbox-renamed").unlink()
            os.fchmod(output.fileno(), 0o640)
            self.assertEqual(output.read(), b"disk file")
        self.assertEqual(list(self.memory.iterdir()), [])

        (self.view / "ordinary-directory").mkdir()
        os.symlink("ordinary-directory", self.view / "sandbox-directory-link")
        (self.view / "sandbox-directory-link/new").write_text("disk")
        self.assertEqual((self.source / "ordinary-directory/new").read_text(), "disk")

    def test_old_disk_sandboxs_are_hidden_and_untouched(self):
        old = self.source / "sandbox_old"
        old.mkdir()
        (old / "keep").write_text("disk data")
        self.assertFalse((self.view / old.name).exists())
        self.assertNotIn(old.name, os.listdir(self.view))
        (self.view / old.name).mkdir()
        (self.view / old.name / "new").write_text("memory data")
        self.assertEqual(os.listdir(self.view / old.name), ["new"])
        shutil.rmtree(self.view / old.name)
        self.assertEqual((old / "keep").read_text(), "disk data")
        self.assertFalse((self.view / old.name).exists())

    def test_repeated_creation_and_recursive_cleanup(self):
        for _ in range(5):
            root = self.view / "sandbox_1"
            (root / "logs").mkdir(parents=True)
            (root / "cache/a").mkdir(parents=True)
            (root / "logs/error.log").write_text("log")
            (root / "cache/a/body").write_bytes(b"body")
            shutil.rmtree(root)
            self.assertFalse((self.memory / root.name).exists())

    def test_open_file_survives_unlink(self):
        root = self.view / "sandbox_2"
        root.mkdir()
        path = root / "body"
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.write(fd, b"request body")
            path.unlink()
            self.assertFalse(path.exists())
            self.assertEqual(os.pread(fd, 12, 0), b"request body")
            os.ftruncate(fd, 7)
            os.fsync(fd)
            os.fchmod(fd, 0o640)
            self.assertEqual(os.fstat(fd).st_size, 7)
            self.assertEqual(stat.S_IMODE(os.fstat(fd).st_mode), 0o640)
        finally:
            os.close(fd)
        # FUSE release can finish after close(2) returns. Unmount before
        # checking that retained data contains no hidden unlinked files.
        with contextlib.redirect_stderr(io.StringIO()):
            self.mount.close(preserve=True)
        self.assertEqual(os.listdir(self.memory / root.name), [])
        self.assertFalse(any(path.name.startswith(".fuse_hidden")
                             for path in self.source.iterdir()))

    def test_mmap_and_metadata(self):
        root = self.view / "sandbox_3"
        root.mkdir()
        path = root / "file"
        path.write_bytes(b"before")
        with path.open("r+b") as output:
            with mmap.mmap(output.fileno(), 0) as mapped:
                mapped[:5] = b"after"
                mapped.flush()
            os.fsync(output.fileno())
        path.chmod(0o640)
        os.utime(path, ns=(123000000000, 456000000000))
        backing = self.memory / root.name / path.name
        self.assertEqual(backing.read_bytes(), b"aftere")
        self.assertEqual(stat.S_IMODE(backing.stat().st_mode), 0o640)
        self.assertEqual(backing.stat().st_mtime_ns, 456000000000)
        os.setxattr(path, "user.sandbox-test", b"value")
        self.assertEqual(os.getxattr(backing, "user.sandbox-test"), b"value")
        self.assertIn("user.sandbox-test", os.listxattr(path))
        os.removexattr(path, "user.sandbox-test")

    def test_capability_probe_bypasses_backend_and_other_attributes_pass_through(self):
        compiler = shutil.which("cc")
        if compiler is None:
            self.skipTest("requires a C compiler for xattr fault injection")
        source = self.mount.base / "xattr-probe.c"
        library = self.mount.base / "xattr-probe.so"
        source.write_text('''#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <string.h>
#include <sys/types.h>
ssize_t lgetxattr(const char *path, const char *name, void *value, size_t size) {
    ssize_t (*real_get)(const char *, const char *, void *, size_t);
    if (strcmp(name, "security.capability") == 0
        || strcmp(name, "security.capability.extra") == 0
        || strcmp(name, "security.selinux") == 0
        || strcmp(name, "system.posix_acl_access") == 0) {
        errno = EACCES;
        return -1;
    }
    real_get = dlsym(RTLD_NEXT, "lgetxattr");
    return real_get(path, name, value, size);
}
''')
        subprocess.run([compiler, "-shared", "-fPIC", str(source), "-o", str(library),
                        "-ldl"], check=True, capture_output=True, timeout=10)
        mount = fs.PrefixMount(self.source, "/tmp", self.binary, ["sandbox"], threads=4)
        self.addCleanup(mount.close)
        original_popen = fs.subprocess.Popen

        def launch(command, **options):
            options["env"] = {**options["env"], "LD_PRELOAD": str(library)}
            return original_popen(command, **options)

        with patch.object(fs.subprocess, "Popen", side_effect=launch):
            mount.start()
        for relative in ("sandbox_xattrs/file", "disk_xattrs/file"):
            with self.subTest(path=relative):
                path = mount.view / relative
                path.parent.mkdir()
                path.write_bytes(b"append")
                with self.assertRaises(OSError) as caught:
                    os.getxattr(path, "security.capability")
                self.assertEqual(caught.exception.errno, errno.ENODATA)
                for name in ("security.capability.extra", "security.selinux",
                             "system.posix_acl_access"):
                    with self.assertRaises(OSError) as caught:
                        os.getxattr(path, name)
                    self.assertEqual(caught.exception.errno, errno.EACCES)
                os.setxattr(path, "user.prefix-test", b"value")
                self.assertEqual(os.getxattr(path, "user.prefix-test"), b"value")
                self.assertIn("user.prefix-test", os.listxattr(path))
                os.removexattr(path, "user.prefix-test")

    def test_links_and_cross_backend_renames(self):
        root = self.view / "sandbox_4"
        root.mkdir()
        path = root / "file"
        path.write_text("memory")
        os.link(path, root / "hardlink")
        self.assertEqual((root / "hardlink").read_text(), "memory")
        os.symlink("sandbox_4/file", self.view / "ordinary-link")
        self.assertEqual((self.view / "ordinary-link").read_text(), "memory")
        self.assertEqual(os.readlink(self.source / "ordinary-link"), "sandbox_4/file")
        with self.assertRaises(OSError) as caught:
            path.rename(self.view / "disk-file")
        self.assertEqual(caught.exception.errno, errno.EXDEV)
        with self.assertRaises(OSError) as caught:
            os.link(path, self.view / "disk-link")
        self.assertEqual(caught.exception.errno, errno.EXDEV)
        self.assertEqual(path.read_text(), "memory")

    def test_large_merged_directory_listing(self):
        expected = set()
        for index in range(1200):
            name = "ordinary_" + str(index)
            (self.source / name).touch()
            expected.add(name)
        for index in range(32):
            name = "sandbox_" + str(index)
            (self.view / name).mkdir()
            expected.add(name)
        (self.source / "sandbox_hidden").mkdir()
        self.assertEqual(set(os.listdir(self.view)), expected)
        self.assertEqual(set(os.listdir(self.view)), expected)

    def test_large_streamed_directory_listing_and_cleanup(self):
        for name in ("ordinary", "sandbox_stream"):
            directory = self.view / name
            directory.mkdir()
            expected = {"entry_" + str(index) for index in range(1500)}
            backing = (self.memory if name.startswith("sandbox") else self.source) / name
            for entry in expected:
                (backing / entry).touch()
            for _ in range(2):
                self.assertEqual(set(os.listdir(directory)), expected)
            # Removing entries during traversal also exercises backing cookies.
            shutil.rmtree(directory)
            self.assertFalse(backing.exists())

    def test_unix_socket_in_sandbox(self):
        root = self.view / "sandbox_5"
        root.mkdir()
        path = root / "backend.sock"
        errors = []
        with socket.socket(socket.AF_UNIX) as listener:
            listener.settimeout(5)
            listener.bind(str(path))
            listener.listen(1)

            def serve():
                try:
                    connection, _ = listener.accept()
                    with connection:
                        connection.settimeout(5)
                        connection.sendall(connection.recv(4))
                except Exception as error:
                    errors.append(error)

            thread = threading.Thread(target=serve)
            thread.start()
            try:
                with socket.socket(socket.AF_UNIX) as client:
                    client.settimeout(5)
                    client.connect(str(path))
                    client.sendall(b"ping")
                    self.assertEqual(client.recv(4), b"ping")
            finally:
                thread.join(timeout=6)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
        self.assertTrue(stat.S_ISSOCK((self.memory / root.name / path.name).stat().st_mode))
        path.unlink()

    def test_parallel_independent_roots(self):
        for count in (1, 2, 4, 8, 0):
            with self.subTest(threads=count):
                mount = self.custom_mount(["sandbox"], threads=count)
                tasks = Path(f"/proc/{mount.daemon.pid}/task")
                expected = 1 if count == 1 else count + 1
                before = set(tasks.iterdir())
                if count:
                    deadline = time.monotonic() + 2
                    while len(before) != expected and time.monotonic() < deadline:
                        time.sleep(0.01)
                        before = set(tasks.iterdir())
                    self.assertEqual(len(before), expected)

                def worker(index):
                    root = mount.view / ("sandbox_" + str(index))
                    root.mkdir()
                    for number in range(20):
                        path = root / str(number)
                        path.write_text(str(index))
                        self.assertEqual(path.read_text(), str(index))
                        path.rename(root / ("renamed-" + str(number)))
                    shutil.rmtree(root)

                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                    list(pool.map(worker, range(16)))
                after = set(tasks.iterdir())
                if count:
                    self.assertEqual(after, before)
                else:
                    self.assertGreater(len(after), 1)
                self.assertEqual(list(mount.memory.iterdir()), [])
        self.assertEqual(list(self.memory.iterdir()), [])
        self.assertEqual(list(self.source.iterdir()), [])

    def test_idle_fixed_pool_terminates_and_unmounts(self):
        for count in (2, 4):
            with self.subTest(threads=count):
                mount = self.custom_mount(["sandbox"], threads=count)
                (mount.view / "ordinary-marker").write_text("preserve")
                mount.daemon.terminate()
                mount.daemon.wait(timeout=5)
                self.assertFalse(fs.is_mounted(mount.view))
                self.assertEqual((self.source / "ordinary-marker").read_text(), "preserve")

    def test_fixed_pool_mixed_operations_from_independent_processes(self):
        with concurrent.futures.ProcessPoolExecutor(max_workers=8) as pool:
            jobs = [pool.submit(churn_files, self.view, worker) for worker in range(16)]
            for job in jobs:
                job.result(timeout=15)
        self.assertEqual(list(self.memory.iterdir()), [])
        self.assertEqual(list(self.source.iterdir()), [])

    def test_close_terminates_daemon_that_does_not_exit_after_unmount(self):
        mount = fs.PrefixMount(self.source, "/tmp", self.binary, ["sandbox"])
        self.addCleanup(mount.close)
        mount.daemon = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        mount.close()
        self.assertLess(mount.daemon.returncode, 0)
        self.assertFalse(mount.base.exists())

    def test_native_helper_rejects_invalid_worker_counts(self):
        for count in ("-1", "65", "bad", "1.5", "", "-18446744073709551615"):
            with self.subTest(threads=count):
                result = subprocess.run(
                    [str(self.binary), "--prefix", "sandbox", "--threads", count,
                     str(self.source), str(self.memory), str(self.mount.base / "unused")],
                    text=True, capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("invalid worker count", result.stderr)

    def test_partial_worker_start_failure_unmounts_and_stops_workers(self):
        compiler = shutil.which("cc")
        if compiler is None:
            self.skipTest("requires a C compiler for pthread fault injection")
        source = self.mount.base / "fail-thread.c"
        library = self.mount.base / "fail-thread.so"
        source.write_text('''#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <pthread.h>
static int calls;
int pthread_create(pthread_t *thread, const pthread_attr_t *attr,
                   void *(*start)(void *), void *arg) {
    int (*real_create)(pthread_t *, const pthread_attr_t *, void *(*)(void *), void *);
    if (++calls == 2) return EAGAIN;
    real_create = dlsym(RTLD_NEXT, "pthread_create");
    return real_create(thread, attr, start, arg);
}
''')
        subprocess.run([compiler, "-shared", "-fPIC", str(source), "-o", str(library),
                        "-ldl", "-pthread"], check=True, capture_output=True, timeout=10)
        mount = fs.PrefixMount(self.source, "/tmp", self.binary, ["sandbox"], threads=4)
        self.addCleanup(mount.close)
        (self.source / "original-marker").write_text("preserve")
        mount.daemon = subprocess.Popen(
            [str(self.binary), "--prefix", "sandbox", "--threads", "4", str(self.source),
             str(mount.memory), str(mount.view), "-f"],
            env={**fs.infrastructure_env(), "LD_PRELOAD": str(library)},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(mount.daemon.stdout.close)
        self.addCleanup(mount.daemon.stderr.close)
        output, errors = mount.daemon.communicate(timeout=5)
        self.assertEqual(mount.daemon.returncode, 1, (output, errors))
        self.assertIn(b"FUSE request workers failed:", errors)
        self.assertFalse(fs.is_mounted(mount.view))
        self.assertEqual((self.source / "original-marker").read_text(), "preserve")

    def test_namespace_preserves_paths_identity_and_disk_writes(self):
        code = '''import os, sys
from pathlib import Path
root = Path(sys.argv[1])
assert os.getuid() == int(sys.argv[2])
assert os.environ['LD_PRELOAD'] == 'libm.so.6'
(root / 'ordinary').write_text('disk')
(root / 'sandbox_namespace').mkdir()
(root / 'sandbox_namespace/file').write_text('memory')
'''
        self.assertEqual(fs.run(self.source, "/tmp",
                               [sys.executable, "-c", code, str(self.source),
                                str(os.getuid())], preload="libm.so.6", prefixes=["sandbox"]), 0)
        self.assertEqual((self.source / "ordinary").read_text(), "disk")
        self.assertFalse((self.source / "sandbox_namespace").exists())
        # The source pathname in this outer namespace was never covered.
        self.assertFalse(fs.is_mounted(self.source))

    def test_command_failure_retains_data_and_log_after_unmount(self):
        mount = fs.PrefixMount(self.source, "/tmp", self.binary, ["sandbox"])
        self.addCleanup(mount.close)
        code = '''from pathlib import Path
import sys
root = Path(sys.argv[1]) / 'sandbox_failed'
(root / 'logs').mkdir(parents=True)
(root / 'logs/error.log').write_text('failure diagnostics')
sys.exit(7)
'''
        output = io.StringIO()
        errors = io.StringIO()
        with patch.object(fs, "PrefixMount", return_value=mount), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status = fs.run(self.source, "/tmp",
                            [sys.executable, "-c", code, str(self.source)],
                            prefixes=["sandbox"])
        self.assertEqual(status, 7)
        self.assertFalse(fs.is_mounted(mount.view))
        self.assertEqual(mount.daemon.returncode, 0)
        self.assertEqual((mount.memory / "sandbox_failed/logs/error.log").read_text(),
                         "failure diagnostics")
        self.assertTrue(mount.log.is_file())
        self.assertIn("retained diagnostics at " + str(mount.base), errors.getvalue())
        self.assertFalse((self.source / "sandbox_failed").exists())

    def test_successful_command_removes_temporary_data(self):
        mount = fs.PrefixMount(self.source, "/tmp", self.binary, ["sandbox"])
        self.addCleanup(mount.close)
        code = '''from pathlib import Path
import sys
root = Path(sys.argv[1]) / 'sandbox_success'
root.mkdir()
(root / 'file').write_text('temporary')
'''
        errors = io.StringIO()
        with patch.object(fs, "PrefixMount", return_value=mount), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(errors):
            status = fs.run(self.source, "/tmp",
                            [sys.executable, "-c", code, str(self.source)],
                            prefixes=["sandbox"])
        self.assertEqual(status, 0)
        self.assertFalse(fs.is_mounted(mount.view))
        self.assertEqual(mount.daemon.returncode, 0)
        self.assertFalse(mount.base.exists())
        self.assertNotIn("retained diagnostics", errors.getvalue())

    def test_mount_failure_retains_the_daemon_log(self):
        helper = self.mount.base / "failed-helper"
        helper.write_text("#!/bin/sh\necho 'mount failure diagnostics' >&2\nexit 1\n")
        helper.chmod(0o755)
        mount = fs.PrefixMount(self.source, "/tmp", helper, ["sandbox"])
        self.addCleanup(mount.close)
        errors = io.StringIO()
        with patch.object(fs, "PrefixMount", return_value=mount), \
                contextlib.redirect_stderr(errors):
            with self.assertRaisesRegex(RuntimeError, "mount failure diagnostics"):
                fs.run(self.source, "/tmp", [sys.executable, "-c", "exit(0)"],
                       prefixes=["sandbox"])
        self.assertFalse(fs.is_mounted(mount.view))
        self.assertIsNotNone(mount.daemon.poll())
        self.assertEqual(mount.log.read_text(), "mount failure diagnostics\n")
        self.assertIn("retained diagnostics at " + str(mount.base), errors.getvalue())

    def test_failed_unmount_never_removes_original_files(self):
        marker = self.source / "keep"
        marker.write_text("original data")
        with patch.object(fs.subprocess, "run"):
            with self.assertRaisesRegex(RuntimeError, "cannot unmount"):
                self.mount.close()
        self.assertEqual(marker.read_text(), "original data")
        self.assertTrue(fs.is_mounted(self.view))
        self.assertIsNone(self.mount.daemon.poll())

    def test_crashed_daemon_is_unmounted_without_following_the_view(self):
        marker = self.source / "keep"
        marker.write_text("original data")
        self.mount.daemon.kill()
        self.mount.daemon.wait(timeout=5)
        self.assertTrue(fs.is_mounted(self.view))
        self.mount.close()
        self.assertFalse(fs.is_mounted(self.view))
        self.assertFalse(self.mount.base.exists())
        self.assertEqual(marker.read_text(), "original data")

    def test_termination_unmounts_and_retains_diagnostics(self):
        code = '''import sys, time
from pathlib import Path
root = Path(sys.argv[1]) / 'sandbox_interrupted'
root.mkdir()
(root / 'diagnostics').write_text('interrupted command')
print('CHILD_READY', flush=True)
time.sleep(60)
'''
        child = subprocess.Popen(
            [sys.executable, str(SCRIPT), "--source", str(self.source), "--prefix", "sandbox", "--",
             sys.executable, "-c", code, str(self.source)], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        self.addCleanup(fs.stop, child)
        self.addCleanup(child.stdout.close)
        self.addCleanup(child.stderr.close)
        line = child.stdout.readline()
        self.assertTrue(line.startswith("prefix-tmpfs: "), line)
        base = Path(line.strip().split(" -> ")[-1]).parent
        self.addCleanup(lambda: shutil.rmtree(base) if base.exists()
                        and not fs.is_mounted(base / "view") else None)
        self.assertEqual(child.stdout.readline().strip(), "CHILD_READY")
        child.terminate()
        self.assertEqual(child.wait(timeout=10), 143, child.stderr.read())
        self.assertFalse(fs.is_mounted(base / "view"))
        self.assertEqual((base / "memory/sandbox_interrupted/diagnostics").read_text(),
                         "interrupted command")
        self.assertTrue((base / "fuse.log").is_file())



if __name__ == "__main__":
    unittest.main()
