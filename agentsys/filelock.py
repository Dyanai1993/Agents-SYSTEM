"""A real OS-level advisory file lock (fcntl.flock on POSIX, msvcrt.locking on Windows).

Review (fourth round): the previous "create a file with O_CREAT|O_EXCL, and if it already exists, check its
mtime and unlink it if it looks stale" scheme had a real race - the mtime check and the unlink are not one
atomic step, so two processes could each see the same lock as stale, and the second one's unconditional
unlink could remove the FIRST one's brand-new, live lock, letting both believe they hold it. Its __exit__
also unlinked the lock file unconditionally, so releasing it could delete a DIFFERENT process's lock that had
since replaced it at that path.

An OS-level advisory lock has neither problem: acquiring it is a single atomic kernel operation (no
check-then-act gap), and it is released automatically when the owning process exits or is killed - even
without clean shutdown - so no staleness heuristic, and no mtime, is needed at all. The lock file itself is
NEVER deleted (only ever created once and left in place): unlinking a path that is currently locked and then
letting a new file be created at the same name would let a third process lock that new, different inode
while the original lock (held via a file descriptor on the now-unlinked inode) is still live elsewhere -
two independent locks on the same path, with no mutual exclusion between them at all.
"""
import os

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class LockBusy(RuntimeError):
    """Another process already holds this lock."""


class FileLock:
    def __init__(self, path):
        self.path = path
        self._fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        try:
            if os.name == "nt":
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            raise LockBusy(f"another process holds {self.path.name}")
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(str(os.getpid()).encode())
            fh.flush()
        except OSError:
            pass          # the lock itself is what matters; failing to record the pid is not fatal
        self._fh = fh
        return self

    def __exit__(self, *exc):
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            if os.name == "nt":
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()
