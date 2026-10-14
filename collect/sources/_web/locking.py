"""跨入口的采集互斥；仅锁运行组，不修改采集内容。"""
import os
from collect.paths import STATE_DIR


def acquire(group):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    handle = (STATE_DIR / f"collect_{group}.lock").open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except OSError:
        handle.close()
        return None
