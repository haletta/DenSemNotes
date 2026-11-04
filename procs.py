"""Programs DenSemNotes starts (Claude Code, Keynote/Pages scripts, LibreOffice, textutil, the update installer).

Every one runs in its own process group and is remembered while it runs, so quitting the app can close them
all at once instead of leaving them behind. An update installer is marked keep=True: it is left to finish,
because stopping it halfway could leave the app half-installed.
"""
import os
import signal
import subprocess
import threading
import time

_lock = threading.Lock()
_live = {}  # pid -> (Popen, keep)


class Result:
    def __init__(self, args, returncode, stdout, stderr):
        self.args, self.returncode, self.stdout, self.stderr = args, returncode, stdout, stderr


def run(cmd, input=None, timeout=None, keep=False, check=False, text=True, **kw):
    """Like subprocess.run (capture_output), but the process can be closed when the app quits."""
    kw.pop("capture_output", None)
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=text, start_new_session=True, **kw)
    with _lock:
        _live[p.pid] = (p, keep)
    try:
        out, err = p.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill(p)
        p.communicate()
        raise
    finally:
        with _lock:
            _live.pop(p.pid, None)
    if check and p.returncode:
        raise subprocess.CalledProcessError(p.returncode, cmd, out, err)
    return Result(cmd, p.returncode, out, err)


def _kill(p, grace=1.0):
    if p.poll() is not None:
        return
    try:
        os.killpg(p.pid, signal.SIGTERM)
    except Exception:
        try:
            p.terminate()
        except Exception:
            pass
    end = time.time() + grace
    while time.time() < end and p.poll() is None:
        time.sleep(0.05)
    if p.poll() is None:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass


def close_all():
    """Close every program the app started, except an update that's being installed. Returns how many."""
    with _lock:
        procs = [p for p, keep in _live.values() if not keep and p.poll() is None]
    threads = [threading.Thread(target=_kill, args=(p,), daemon=True) for p in procs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=2)
    return len(procs)


def running(keep=None):
    with _lock:
        return [p for p, k in _live.values() if p.poll() is None and (keep is None or k == keep)]
