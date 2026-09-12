"""长期运行的日志落盘。

**为什么要有这个模块**：原先调度器所有输出都是 `print` 到 stdout，进程一关
（重启、被 kill、计划任务跑完）日志就彻底没了。长期运营最需要的能力恰恰是
回溯 —— "昨晚 3 点那轮采集为什么失败""这个关键词连续几天没产出"。没有文件
日志，这些问题只能靠猜。

做法：
1. 给 root logger 挂一个 RotatingFileHandler，**轮转**（默认 10MB × 10 份），
   否则长期跑必然把磁盘撑爆。
2. 把 `sys.stdout/stderr` 换成 Tee，顺带捕获底层 `print`（采集子进程的进度、
   流水线各层的打印），让它们也进同一个文件。

只应在**长驻进程**（调度器）启动时调用；CLI 一次性命令没必要落日志。
"""

from __future__ import annotations

import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from Whochat.config import DATA_DIR

LOG_DIR = DATA_DIR / "logs"
LOG_FILE = LOG_DIR / "Whochat.log"

_MAX_BYTES = 10 * 1024 * 1024
_BACKUP_COUNT = 10


class _Tee:
    """把写向原流的内容同时复制到日志文件。

    线程安全：调度器用线程池跑采集，多线程同时 print 会交错甚至撕裂行，
    因此写入加锁。
    """

    def __init__(self, original, log_stream, lock: threading.Lock):
        self._original = original
        self._log = log_stream
        self._lock = lock

    def write(self, data):
        if not data:
            return 0
        with self._lock:
            try:
                self._log.write(data)
                self._log.flush()
            except Exception:
                # 日志失败绝不能影响主流程（磁盘满、文件被占用等）
                pass
        return self._original.write(data)

    def flush(self):
        with self._lock:
            try:
                self._log.flush()
            except Exception:
                pass
        self._original.flush()

    def __getattr__(self, name):
        return getattr(self._original, name)


_installed = False


def setup_logging(level: int = logging.INFO) -> Path:
    """配置文件日志并接管 stdout/stderr。幂等，可重复调用。返回日志文件路径。"""
    global _installed
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    handler = RotatingFileHandler(
        LOG_FILE, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.setLevel(level)
    # 避免重复安装 handler（例如测试或反复调用）
    if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        root.addHandler(handler)

    if not _installed:
        lock = threading.Lock()
        try:
            log_stream = open(LOG_FILE, "a", encoding="utf-8", errors="replace")
        except OSError:
            # 打不开就只保留 logging 那一路，不因为日志问题让调度器起不来
            return LOG_FILE
        sys.stdout = _Tee(sys.stdout, log_stream, lock)  # type: ignore[assignment]
        sys.stderr = _Tee(sys.stderr, log_stream, lock)  # type: ignore[assignment]
        _installed = True

    return LOG_FILE
