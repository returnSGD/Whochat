"""控制台输出兼容。

Windows 默认控制台编码是 GBK，编码不了 emoji（🔴🟠📊）。而本项目的推送文案
（企微 markdown）**必须**保留 emoji —— 那是发给群里的，UTF-8 完全没问题。

问题出在 dry-run：没配 webhook 时我们会把同样的文案打印到本地控制台，
`print()` 在 GBK stdout 上抛 UnicodeEncodeError，把整条命令打断。
实测 `python -m wochat.cli demo`（README 的头号命令）就在
`notifier.flush()` 的 dry-run 打印处崩掉，退出码 1，后面的词云/汇总全没跑。

修法不是删 emoji（会牺牲推送观感），而是把 stdout 的错误处理从 strict 改成
replace：GBK 能编码的中文照常显示，编不了的 emoji 退化成 '?'，不再崩。
"""

from __future__ import annotations

import sys
from typing import Any


def configure_console(stream: Any = None) -> None:
    """让控制台在遇到无法编码的字符时降级而不是抛异常。

    幂等，可重复调用。stdout 被 pytest / 重定向接管（没有 reconfigure 或不可
    重配）时静默跳过 —— 那种情况下打印本来也不会经过 GBK 编码。
    """
    stream = stream if stream is not None else sys.stdout
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        reconfigure(errors="replace")
    except (ValueError, OSError):
        # 流已被关闭 / 不可重配，忽略
        pass
