/* Prefix-routed filesystem: selected directories live on tmpfs, other data
 * is passed through to the original directory. No copy-up or whiteouts. */
#define _GNU_SOURCE
#define FUSE_USE_VERSION 31

#include <fuse.h>
#include <fuse_lowlevel.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <math.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <sys/statfs.h>
#include <sys/statvfs.h>
#include <sys/syscall.h>
#include <sys/xattr.h>
#include <unistd.h>

static int source_fd = -1;
static int memory_fd = -1;

struct prefix {
    const char *value;
    size_t length;
};

static const struct prefix *prefixes;
static size_t prefix_count;
static double cache_timeout;
static unsigned int worker_count;

#define MAX_WORKERS 64

struct entry {
    char *name;
    ino_t ino;
    mode_t mode;
};

struct handle {
    int fd;
    DIR *directory;
    struct entry *entries;
    size_t count;
    size_t capacity;
};

static int
matches_prefix(const char *name)
{
    size_t i;

    for (i = 0; i < prefix_count; i++) {
        if (strncmp(name, prefixes[i].value, prefixes[i].length) == 0) {
            return 1;
        }
    }

    return 0;
}

static int
lookup_top(const char *name, struct stat *st, int *backend)
{
    int error;

    *backend = memory_fd;
    if (fstatat(memory_fd, name, st, AT_SYMLINK_NOFOLLOW) == 0) {
        return 0;
    }

    error = errno;
    if (fstatat(source_fd, name, st, AT_SYMLINK_NOFOLLOW) == 0
        && !S_ISDIR(st->st_mode))
    {
        *backend = source_fd;
        return 0;
    }

    return -error;
}

static int
route(const char *path, const char **relative)
{
    struct stat st;
    int backend;

    if (path == NULL || path[0] != '/') {
        return -EINVAL;
    }

    *relative = path[1] ? path + 1 : ".";
    if (!matches_prefix(*relative)) {
        return source_fd;
    }

    if (strchr(*relative, '/') != NULL) {
        return memory_fd;
    }

    /* The policy selects directories, not prefix-matching ordinary files.
     * Keep root-level regular files and symlinks on their original backend. */
    lookup_top(*relative, &st, &backend);
    return backend;
}

static int
file_destination(const char *path, const char **relative)
{
    int fd = route(path, relative);

    if (fd >= 0 && matches_prefix(*relative) && strchr(*relative, '/') == NULL) {
        return source_fd;
    }

    return fd;
}

static struct handle *
get_handle(struct fuse_file_info *fi)
{
    return (struct handle *) (uintptr_t) fi->fh;
}

static int
save_handle(int fd, struct fuse_file_info *fi)
{
    struct handle *h;

    if (fd < 0) {
        return -errno;
    }

    h = calloc(1, sizeof(*h));
    if (h == NULL) {
        close(fd);
        return -ENOMEM;
    }

    h->fd = fd;
    fi->fh = (uint64_t) (uintptr_t) h;
    return 0;
}

static int
sfs_getattr(const char *path, struct stat *st, struct fuse_file_info *fi)
{
    const char *rel;
    int fd;

    if (fi != NULL) {
        return fstat(get_handle(fi)->fd, st) == -1 ? -errno : 0;
    }

    /* Routing a selected root already reads its attributes. Reuse them
     * instead of issuing the same fstatat() a second time. */
    if (path != NULL && path[0] == '/' && matches_prefix(path + 1)
        && strchr(path + 1, '/') == NULL)
    {
        return lookup_top(path + 1, st, &fd);
    }

    fd = route(path, &rel);
    if (fd < 0) {
        return fd;
    }

    return fstatat(fd, rel, st, AT_SYMLINK_NOFOLLOW) == -1 ? -errno : 0;
}

static int
sfs_readlink(const char *path, char *buf, size_t size)
{
    const char *rel;
    int fd = route(path, &rel);
    ssize_t n;

    if (fd < 0 || size == 0) {
        return fd < 0 ? fd : -EINVAL;
    }

    n = readlinkat(fd, rel, buf, size - 1);
    if (n < 0) {
        return -errno;
    }

    buf[n] = '\0';
    return 0;
}

static int
sfs_mkdir(const char *path, mode_t mode)
{
    const char *rel;
    int fd = route(path, &rel);
    return fd < 0 ? fd : (mkdirat(fd, rel, mode) == -1 ? -errno : 0);
}

static int
sfs_mknod(const char *path, mode_t mode, dev_t device)
{
    const char *rel;
    int fd = file_destination(path, &rel);
    return fd < 0 ? fd : (mknodat(fd, rel, mode, device) == -1 ? -errno : 0);
}

static int
sfs_unlink(const char *path)
{
    const char *rel;
    int fd = route(path, &rel);
    return fd < 0 ? fd : (unlinkat(fd, rel, 0) == -1 ? -errno : 0);
}

static int
sfs_rmdir(const char *path)
{
    const char *rel;
    int fd = route(path, &rel);
    return fd < 0 ? fd : (unlinkat(fd, rel, AT_REMOVEDIR) == -1 ? -errno : 0);
}

static int
sfs_symlink(const char *target, const char *path)
{
    const char *rel;
    int fd = file_destination(path, &rel);
    return fd < 0 ? fd : (symlinkat(target, fd, rel) == -1 ? -errno : 0);
}

static int
sfs_rename(const char *from, const char *to, unsigned int flags)
{
    const char *old_rel = NULL, *new_rel = NULL;
    struct stat st;
    int old_fd = route(from, &old_rel);
    int new_fd = route(to, &new_rel);

    if (old_fd < 0 || new_fd < 0) {
        return old_fd < 0 ? old_fd : new_fd;
    }

    if (fstatat(old_fd, old_rel, &st, AT_SYMLINK_NOFOLLOW) == -1) {
        return -errno;
    }

    if (!S_ISDIR(st.st_mode)) {
        new_fd = file_destination(to, &new_rel);
    }

    if (old_fd != new_fd) {
        return -EXDEV;
    }

    return syscall(SYS_renameat2, old_fd, old_rel, new_fd, new_rel, flags) == -1
           ? -errno : 0;
}

static int
sfs_link(const char *from, const char *to)
{
    const char *old_rel = NULL, *new_rel = NULL;
    int old_fd = route(from, &old_rel);
    int new_fd = file_destination(to, &new_rel);

    if (old_fd < 0 || new_fd < 0) {
        return old_fd < 0 ? old_fd : new_fd;
    }

    if (old_fd != new_fd) {
        return -EXDEV;
    }

    return linkat(old_fd, old_rel, new_fd, new_rel, 0) == -1 ? -errno : 0;
}

static int
sfs_chmod(const char *path, mode_t mode, struct fuse_file_info *fi)
{
    const char *rel;
    int fd;

    if (fi != NULL) {
        return fchmod(get_handle(fi)->fd, mode) == -1 ? -errno : 0;
    }

    fd = route(path, &rel);
    return fd < 0 ? fd : (fchmodat(fd, rel, mode, 0) == -1 ? -errno : 0);
}

static int
sfs_chown(const char *path, uid_t uid, gid_t gid, struct fuse_file_info *fi)
{
    const char *rel;
    int fd;

    if (fi != NULL) {
        return fchown(get_handle(fi)->fd, uid, gid) == -1 ? -errno : 0;
    }

    fd = route(path, &rel);
    return fd < 0 ? fd
           : (fchownat(fd, rel, uid, gid, AT_SYMLINK_NOFOLLOW) == -1 ? -errno : 0);
}

static int
sfs_open(const char *path, struct fuse_file_info *fi)
{
    const char *rel;
    int fd = route(path, &rel);
    return fd < 0 ? fd : save_handle(openat(fd, rel, fi->flags | O_CLOEXEC), fi);
}

static int
sfs_create(const char *path, mode_t mode, struct fuse_file_info *fi)
{
    const char *rel;
    int fd = file_destination(path, &rel);
    return fd < 0 ? fd
           : save_handle(openat(fd, rel, fi->flags | O_CREAT | O_CLOEXEC, mode), fi);
}

static int
sfs_read(const char *path, char *buf, size_t size, off_t offset,
    struct fuse_file_info *fi)
{
    ssize_t n;
    (void) path;
    n = pread(get_handle(fi)->fd, buf, size, offset);
    return n < 0 ? -errno : (int) n;
}

static int
sfs_write(const char *path, const char *buf, size_t size, off_t offset,
    struct fuse_file_info *fi)
{
    ssize_t n;
    (void) path;
    n = pwrite(get_handle(fi)->fd, buf, size, offset);
    return n < 0 ? -errno : (int) n;
}

static int
sfs_truncate(const char *path, off_t size, struct fuse_file_info *fi)
{
    const char *rel;
    int fd, result, error;

    if (fi != NULL) {
        return ftruncate(get_handle(fi)->fd, size) == -1 ? -errno : 0;
    }

    fd = route(path, &rel);
    if (fd < 0) {
        return fd;
    }

    fd = openat(fd, rel, O_WRONLY | O_CLOEXEC);
    if (fd == -1) {
        return -errno;
    }

    result = ftruncate(fd, size);
    error = errno;
    close(fd);
    return result == -1 ? -error : 0;
}

static int
sfs_flush(const char *path, struct fuse_file_info *fi)
{
    int fd;
    (void) path;
    fd = dup(get_handle(fi)->fd);
    return fd == -1 ? -errno : (close(fd) == -1 ? -errno : 0);
}

static int
sfs_fsync(const char *path, int data_only, struct fuse_file_info *fi)
{
    int result;
    (void) path;
    result = data_only ? fdatasync(get_handle(fi)->fd) : fsync(get_handle(fi)->fd);
    return result == -1 ? -errno : 0;
}

static int
sfs_release(const char *path, struct fuse_file_info *fi)
{
    struct handle *h = get_handle(fi);
    size_t i;
    int result;
    (void) path;

    for (i = 0; i < h->count; i++) {
        free(h->entries[i].name);
    }

    free(h->entries);
    result = h->directory != NULL ? closedir(h->directory) : close(h->fd);
    free(h);
    return result == -1 ? -errno : 0;
}

static int
sfs_utimens(const char *path, const struct timespec times[2],
    struct fuse_file_info *fi)
{
    const char *rel;
    int fd;

    if (fi != NULL) {
        return futimens(get_handle(fi)->fd, times) == -1 ? -errno : 0;
    }

    fd = route(path, &rel);
    return fd < 0 ? fd
           : (utimensat(fd, rel, times, AT_SYMLINK_NOFOLLOW) == -1 ? -errno : 0);
}

static int
sfs_statfs(const char *path, struct statvfs *st)
{
    const char *rel;
    int fd = route(path, &rel);
    return fd < 0 ? fd : (fstatvfs(fd, st) == -1 ? -errno : 0);
}

static int
add_entries(struct handle *h, int fd, int filter)
{
    DIR *dir;
    struct dirent *de;
    struct entry *entries;
    size_t capacity;
    struct stat st;
    int error = 0;

    dir = fdopendir(fd);
    if (dir == NULL) {
        error = errno;
        close(fd);
        return -error;
    }

    for (;;) {
        errno = 0;
        de = readdir(dir);
        if (de == NULL) {
            error = errno;
            break;
        }

        if (filter == 2 && !matches_prefix(de->d_name)) {
            continue;
        }

        if (filter == 1 && matches_prefix(de->d_name)) {
            if ((fstatat(dirfd(dir), de->d_name, &st, AT_SYMLINK_NOFOLLOW) == 0
                 && S_ISDIR(st.st_mode))
                || fstatat(memory_fd, de->d_name, &st, AT_SYMLINK_NOFOLLOW) == 0)
            {
                continue;
            }
        }

        if (h->count == h->capacity) {
            capacity = h->capacity ? h->capacity * 2 : 32;
            entries = realloc(h->entries, capacity * sizeof(*entries));
            if (entries == NULL) {
                error = ENOMEM;
                break;
            }

            h->entries = entries;
            h->capacity = capacity;
        }

        h->entries[h->count].name = strdup(de->d_name);
        if (h->entries[h->count].name == NULL) {
            error = ENOMEM;
            break;
        }

        h->entries[h->count].ino = de->d_ino;
        h->entries[h->count].mode = ((mode_t) de->d_type) << 12;
        h->count++;
    }

    closedir(dir);
    return -error;
}

static int
sfs_opendir(const char *path, struct fuse_file_info *fi)
{
    const char *rel;
    int fd = route(path, &rel);
    int result, scan_fd;
    struct handle *h;

    if (fd < 0) {
        return fd;
    }

    result = save_handle(openat(fd, rel, O_RDONLY | O_DIRECTORY | O_CLOEXEC), fi);
    if (result < 0) {
        return result;
    }

    h = get_handle(fi);
    if (path[1] != '\0') {
        /* Only the merged root needs a snapshot. Stream all other
         * directories directly, using the backing filesystem's cookies. */
        h->directory = fdopendir(h->fd);
        if (h->directory == NULL) {
            result = -errno;
            sfs_release(path, fi);
            return result;
        }

        return 0;
    }

    scan_fd = openat(h->fd, ".", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    result = scan_fd < 0 ? -errno : add_entries(h, scan_fd, path[1] ? 0 : 1);
    if (result == 0 && path[1] == '\0') {
        scan_fd = openat(memory_fd, ".", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
        result = scan_fd < 0 ? -errno : add_entries(h, scan_fd, 2);
    }

    if (result < 0) {
        sfs_release(path, fi);
    }

    return result;
}

static int
sfs_readdir(const char *path, void *buf, fuse_fill_dir_t filler, off_t offset,
    struct fuse_file_info *fi, enum fuse_readdir_flags flags)
{
    struct handle *h = get_handle(fi);
    struct stat st;
    struct dirent *de;
    off_t next;
    size_t i;
    (void) path;
    (void) flags;

    if (offset < 0) {
        return -EINVAL;
    }

    if (h->directory != NULL) {
        seekdir(h->directory, offset);
        for (;;) {
            errno = 0;
            de = readdir(h->directory);
            if (de == NULL) {
                return -errno;
            }

            next = telldir(h->directory);
            memset(&st, 0, sizeof(st));
            st.st_ino = de->d_ino;
            st.st_mode = ((mode_t) de->d_type) << 12;
            if (filler(buf, de->d_name, &st, next, 0)) {
                break;
            }
        }

        return 0;
    }

    for (i = (size_t) offset; i < h->count; i++) {
        memset(&st, 0, sizeof(st));
        st.st_ino = h->entries[i].ino;
        st.st_mode = h->entries[i].mode;
        if (filler(buf, h->entries[i].name, &st, (off_t) i + 1, 0)) {
            break;
        }
    }

    return 0;
}

/* xattr calls have no *at variant; retain access through the held directory
 * descriptor, even when the view is mounted over the original pathname. */
static int
xattr_path(const char *path, char *buf, size_t size)
{
    const char *rel;
    int fd = route(path, &rel);
    int n;

    if (fd < 0) {
        return fd;
    }

    n = snprintf(buf, size, "/proc/self/fd/%d/%s", fd, rel);
    return n < 0 || (size_t) n >= size ? -ENAMETOOLONG : 0;
}

static int
sfs_getxattr(const char *path, const char *name, char *value, size_t size)
{
    char full[PATH_MAX + 64];
    int result = xattr_path(path, full, sizeof(full));
    ssize_t n;

    if (result < 0) {
        return result;
    }

    n = lgetxattr(full, name, value, size);
    return n < 0 ? -errno : (int) n;
}

static int
sfs_listxattr(const char *path, char *list, size_t size)
{
    char full[PATH_MAX + 64];
    int result = xattr_path(path, full, sizeof(full));
    ssize_t n;

    if (result < 0) {
        return result;
    }

    n = llistxattr(full, list, size);
    return n < 0 ? -errno : (int) n;
}

static int
sfs_setxattr(const char *path, const char *name, const char *value, size_t size,
    int flags)
{
    char full[PATH_MAX + 64];
    int result = xattr_path(path, full, sizeof(full));
    return result < 0 ? result
           : (lsetxattr(full, name, value, size, flags) == -1 ? -errno : 0);
}

static int
sfs_removexattr(const char *path, const char *name)
{
    char full[PATH_MAX + 64];
    int result = xattr_path(path, full, sizeof(full));
    return result < 0 ? result : (lremovexattr(full, name) == -1 ? -errno : 0);
}

static int
sfs_fallocate(const char *path, int mode, off_t offset, off_t length,
    struct fuse_file_info *fi)
{
    (void) path;
    return fallocate(get_handle(fi)->fd, mode, offset, length) == -1 ? -errno : 0;
}

static off_t
sfs_lseek(const char *path, off_t offset, int whence, struct fuse_file_info *fi)
{
    off_t result;
    (void) path;
    result = lseek(get_handle(fi)->fd, offset, whence);
    return result == -1 ? -errno : result;
}

static int
sfs_flock(const char *path, struct fuse_file_info *fi, int operation)
{
    (void) path;
    return flock(get_handle(fi)->fd, operation) == -1 ? -errno : 0;
}

static void *
sfs_init(struct fuse_conn_info *conn, struct fuse_config *cfg)
{
    (void) conn;
    cfg->entry_timeout = cache_timeout;
    cfg->negative_timeout = 0;
    cfg->attr_timeout = cache_timeout;
    cfg->kernel_cache = 0;
    cfg->auto_cache = 1;
    /* Check backing file data on every open, even when metadata caching
     * is enabled. Do not add another userspace attribute-cache window. */
    cfg->ac_attr_timeout_set = 1;
    cfg->ac_attr_timeout = 0;
    cfg->nullpath_ok = 1;
    /* Retain libfuse's hidden-name handling for open, unlinked files. This
     * also preserves fchmod/fchown, whose kernel requests may omit the fh. */
    cfg->hard_remove = 0;
    return NULL;
}

static const struct fuse_operations operations = {
    .getattr = sfs_getattr,
    .readlink = sfs_readlink,
    .mknod = sfs_mknod,
    .mkdir = sfs_mkdir,
    .unlink = sfs_unlink,
    .rmdir = sfs_rmdir,
    .symlink = sfs_symlink,
    .rename = sfs_rename,
    .link = sfs_link,
    .chmod = sfs_chmod,
    .chown = sfs_chown,
    .truncate = sfs_truncate,
    .open = sfs_open,
    .read = sfs_read,
    .write = sfs_write,
    .statfs = sfs_statfs,
    .flush = sfs_flush,
    .release = sfs_release,
    .fsync = sfs_fsync,
    .opendir = sfs_opendir,
    .readdir = sfs_readdir,
    .releasedir = sfs_release,
    .fsyncdir = sfs_fsync,
    .init = sfs_init,
    .create = sfs_create,
    .utimens = sfs_utimens,
    .getxattr = sfs_getxattr,
    .listxattr = sfs_listxattr,
    .setxattr = sfs_setxattr,
    .removexattr = sfs_removexattr,
    .fallocate = sfs_fallocate,
    .lseek = sfs_lseek,
    .flock = sfs_flock,
};

struct worker {
    struct fuse_session *session;
    struct fuse_buf buffer;
    pthread_t thread;
    int notify_fd;
    int result;
};

static void
free_worker_buffer(void *data)
{
    struct worker *worker = data;
    free(worker->buffer.mem);
}

static void *
serve_requests(void *data)
{
    struct worker *worker = data;
    int result;
    char finished = 1;

    /* Cancellation is safe while waiting for requests, never while a
     * callback or libfuse holds a lock. Each worker reuses its buffer. */
    pthread_setcancelstate(PTHREAD_CANCEL_DISABLE, NULL);
    pthread_cleanup_push(free_worker_buffer, worker);
    while (!fuse_session_exited(worker->session)) {
        pthread_setcancelstate(PTHREAD_CANCEL_ENABLE, NULL);
        result = fuse_session_receive_buf(worker->session, &worker->buffer);
        pthread_setcancelstate(PTHREAD_CANCEL_DISABLE, NULL);
        if (result == -EINTR) {
            continue;
        }

        if (result <= 0) {
            worker->result = result;
            break;
        }

        fuse_session_process_buf(worker->session, &worker->buffer);
    }

    fuse_session_exit(worker->session);
    /* There are at most MAX_WORKERS notifications, so the nonblocking
     * pipe cannot fill. Wake the coordinator on EOF or receive failure. */
    (void) write(worker->notify_fd, &finished, sizeof(finished));
    pthread_cleanup_pop(1);
    return NULL;
}

static int
fixed_loop(struct fuse_session *session, unsigned int count)
{
    struct worker workers[MAX_WORKERS] = {0};
    struct pollfd wake;
    sigset_t blocked, previous;
    unsigned int created = 0, i;
    int notifications[2], error, result = 0;

    if (pipe2(notifications, O_CLOEXEC | O_NONBLOCK) == -1) {
        return -errno;
    }

    sigemptyset(&blocked);
    sigaddset(&blocked, SIGINT);
    sigaddset(&blocked, SIGTERM);
    sigaddset(&blocked, SIGHUP);
    sigaddset(&blocked, SIGQUIT);
    error = pthread_sigmask(SIG_BLOCK, &blocked, &previous);
    if (error != 0) {
        result = -error;
        goto done;
    }

    for (i = 0; i < count; i++) {
        workers[i].session = session;
        workers[i].notify_fd = notifications[1];
        error = pthread_create(&workers[i].thread, NULL, serve_requests, &workers[i]);
        if (error != 0) {
            result = -error;
            break;
        }

        created++;
    }

    wake.fd = notifications[0];
    wake.events = POLLIN;
    while (result == 0 && !fuse_session_exited(session)) {
        /* Atomically unblock termination signals while waiting. A plain
         * poll could miss a signal between checking exited and sleeping. */
        if (ppoll(&wake, 1, NULL, &previous) >= 0) {
            break;
        }

        if (errno != EINTR) {
            result = -errno;
        }
    }

    fuse_session_exit(session);
    for (i = 0; i < created; i++) {
        pthread_cancel(workers[i].thread);
    }

    for (i = 0; i < created; i++) {
        pthread_join(workers[i].thread, NULL);
        if (workers[i].result < 0) {
            result = workers[i].result;
        }
    }
    pthread_sigmask(SIG_SETMASK, &previous, NULL);

done:
    close(notifications[0]);
    close(notifications[1]);
    return result;
}

static int
bounded_main(int argc, char **argv)
{
    struct fuse_args args = FUSE_ARGS_INIT(argc, argv);
    struct fuse_cmdline_opts options;
    struct fuse *fuse;
    struct fuse_session *session;
    int result = 1;

    if (fuse_parse_cmdline(&args, &options) != 0) {
        fuse_opt_free_args(&args);
        free(options.mountpoint);
        return 1;
    }

    if (options.show_help || options.show_version) {
        /* Preserve libfuse's help/version behavior for native callers. */
        result = fuse_main(argc, argv, &operations, NULL);
        goto free_args;
    }

    if (options.mountpoint == NULL) {
        fprintf(stderr, "a mountpoint is required\n");
        goto free_args;
    }

    fuse = fuse_new(&args, &operations, sizeof(operations), NULL);
    if (fuse == NULL) {
        goto free_args;
    }

    if (fuse_mount(fuse, options.mountpoint) != 0) {
        goto destroy;
    }

    if (fuse_daemonize(options.foreground) != 0) {
        goto unmount;
    }

    session = fuse_get_session(fuse);
    if (fuse_set_signal_handlers(session) != 0) {
        goto unmount;
    }

    if (worker_count == 1 || options.singlethread) {
        result = fuse_loop(fuse);
    } else if (fuse_start_cleanup_thread(fuse) == 0) {
        result = fixed_loop(session, worker_count);
        fuse_stop_cleanup_thread(fuse);
        if (result < 0) {
            fprintf(stderr, "FUSE request workers failed: %s\n", strerror(-result));
        }
    }

    fuse_remove_signal_handlers(session);
unmount:
    fuse_unmount(fuse);
destroy:
    fuse_destroy(fuse);
free_args:
    free(options.mountpoint);
    fuse_opt_free_args(&args);
    return result != 0;
}

int
main(int argc, char **argv)
{
    struct statfs st;
    struct stat source, memory;
    struct prefix *configured;
    const char *value;
    char *end;
    size_t count = 0, length;
    int first = 1;
    int result = 2;

    if (argc < 4) {
        fprintf(stderr, "usage: %s [--prefix NAME]... [--cache-timeout SECONDS] "
                "[--threads 0..64] "
                "SOURCE TMPFS MOUNTPOINT "
                "[FUSE options]\n", argv[0]);
        return 2;
    }

    configured = calloc((size_t) argc, sizeof(*configured));
    if (configured == NULL) {
        perror("allocate prefixes");
        return 1;
    }

    while (first < argc && (strcmp(argv[first], "--prefix") == 0
                           || strcmp(argv[first], "--cache-timeout") == 0
                           || strcmp(argv[first], "--threads") == 0))
    {
        if (first + 1 == argc) {
            fprintf(stderr, "%s requires a value\n", argv[first]);
            goto done;
        }

        value = argv[first + 1];
        if (strcmp(argv[first], "--threads") == 0) {
            long parsed;
            errno = 0;
            parsed = strtol(value, &end, 10);
            if (end == value || *end != '\0' || errno != 0
                || parsed < 0 || parsed > MAX_WORKERS)
            {
                fprintf(stderr, "invalid worker count: %s\n", value);
                goto done;
            }

            worker_count = (unsigned int) parsed;
            first += 2;
            continue;
        }

        if (strcmp(argv[first], "--cache-timeout") == 0) {
            errno = 0;
            cache_timeout = strtod(value, &end);
            if (end == value || *end != '\0' || errno != 0
                || !isfinite(cache_timeout) || cache_timeout < 0)
            {
                fprintf(stderr, "invalid cache timeout: %s\n", value);
                goto done;
            }

            first += 2;
            continue;
        }

        length = strlen(value);
        if (length == 0 || length > NAME_MAX || strchr(value, '/') != NULL
            || strcmp(value, ".") == 0 || strcmp(value, "..") == 0)
        {
            fprintf(stderr, "invalid directory-name prefix: %s\n", value);
            goto done;
        }

        configured[count].value = value;
        configured[count++].length = length;
        first += 2;
    }

    if (argc - first < 3) {
        fprintf(stderr, "SOURCE, TMPFS and MOUNTPOINT are required\n");
        goto done;
    }

    if (count == 0) {
        fprintf(stderr, "at least one --prefix is required\n");
        goto done;
    }

    prefixes = configured;
    prefix_count = count;

    result = 1;
    source_fd = open(argv[first], O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    memory_fd = open(argv[first + 1], O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (source_fd == -1 || memory_fd == -1
        || fstatfs(memory_fd, &st) == -1
        || fstat(source_fd, &source) == -1 || fstat(memory_fd, &memory) == -1)
    {
        perror("open backing directories");
        goto done;
    }

    if ((unsigned long) st.f_type != 0x01021994UL) {
        fprintf(stderr, "TMPFS backing directory must be on tmpfs\n");
        goto done;
    }

    if (source.st_dev == memory.st_dev && source.st_ino == memory.st_ino) {
        fprintf(stderr, "backing directories must be distinct\n");
        goto done;
    }

    /* FUSE supplies the caller's already-masked mode for creations. */
    umask(0);
    argv[first + 1] = argv[0];
    if (worker_count == 0) {
        result = fuse_main(argc - first - 1, argv + first + 1, &operations, NULL);
    } else {
        result = bounded_main(argc - first - 1, argv + first + 1);
    }

done:
    if (memory_fd >= 0) {
        close(memory_fd);
    }

    if (source_fd >= 0) {
        close(source_fd);
    }

    free(configured);
    return result;
}
