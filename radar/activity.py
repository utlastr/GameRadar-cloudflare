"""程序当前在做什么 + 最近的运行日志。给面板的"运行情况"和托盘图标显示用。"""
import collections
import sys
import threading
import time

_lock = threading.Lock()
_lines = collections.deque(maxlen=1000)
_seq = 0
_tasks = {}
_state = {"busy": False, "task": "启动中", "detail": "", "done": 0, "total": 0, "started": None,
          "next_live": None, "next_slow": None, "rev": 0}


def log(msg):
    """记一行日志（不打印）。"""
    global _seq
    msg = str(msg).rstrip()
    if not msg:
        return
    with _lock:
        for line in msg.splitlines():
            _seq += 1
            _lines.append({"seq": _seq, "ts": time.time(), "msg": line})


def say(msg):
    """打印一行带时间的日志（会写进日志文件，也会显示在面板上）。"""
    print(f"{time.strftime('%H:%M:%S')}  {msg}")


def begin(task, detail=""):
    with _lock:
        _state.update(busy=True, task=task, detail=detail, done=0, total=0, started=time.time())
        _tasks[threading.get_ident()] = {k: _state[k] for k in
                                        ("task", "detail", "done", "total", "started")}


def progress(done, total, detail=None):
    with _lock:
        _state["done"], _state["total"] = done, total
        if detail is not None:
            _state["detail"] = detail
        task = _tasks.get(threading.get_ident())
        if task is not None:
            task.update(done=done, total=total)
            if detail is not None:
                task["detail"] = detail


def end():
    with _lock:
        _state.update(busy=False, done=0, total=0, detail="")
        _tasks.pop(threading.get_ident(), None)
        if _tasks:
            _state.update(next(reversed(_tasks.values())), busy=True)
        _state["rev"] += 1


def restore(saved):
    """插空做完别的事以后，把"正在做什么"恢复成之前的样子。"""
    with _lock:
        for k in ("busy", "task", "detail", "done", "total", "started"):
            if k in saved:
                _state[k] = saved[k]
        _state["rev"] += 1


def idle(next_live, next_slow):
    with _lock:
        if _tasks:
            _state.update(next(reversed(_tasks.values())), busy=True,
                          next_live=next_live, next_slow=next_slow)
            return
        _state.update(busy=False, task="空闲", detail="", done=0, total=0,
                      next_live=next_live, next_slow=next_slow)


def state():
    with _lock:
        return dict(_state, tasks=list(_tasks.values()))


def snapshot(after=0):
    with _lock:
        return {"state": dict(_state, tasks=list(_tasks.values())), "seq": _seq,
                "lines": [l for l in _lines if l["seq"] > after][-500:]}


class Tee:
    """print 的内容同时写到日志文件（或原来的窗口），并记进最近日志。"""

    def __init__(self, *streams):
        self.streams = [s for s in streams if s is not None]
        self._buf = ""
        self._wlock = threading.Lock()

    def write(self, s):
        for st in self.streams:
            try:
                st.write(s)
            except Exception:
                pass
        with self._wlock:
            self._buf += s
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                log(line)
        return len(s)

    def flush(self):
        for st in self.streams:
            try:
                st.flush()
            except Exception:
                pass


def install(*streams):
    """把 sys.stdout / sys.stderr 换成 Tee。"""
    if isinstance(sys.stdout, Tee):
        return
    tee = Tee(*(streams or (sys.__stdout__,)))
    sys.stdout = tee
    sys.stderr = tee
