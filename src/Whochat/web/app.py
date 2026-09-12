"""可视化入口 —— 双页导航。

    看板端（dashboard.py）  只读：趋势、情感、主题、词云、传播、预警记录
    操作端（console.py）    可写：L1~L5 的触发与微调（采集/分析/规则/推送）

两者刻意分开（方案文档 §5.1 的存储/展示边界）：看板是长时间开着"随手看一眼"
的页面，操作端是改参数、触发动作的地方。混在一起，一个误点就可能重跑分析
或改掉预警规则。

启动：
    python -m Whochat.cli dashboard      # → http://localhost:8501
    # 直接打开某个入口：/ （看板端）或 /console （操作端）
"""

from __future__ import annotations

import sys
from pathlib import Path

# Streamlit 直接 `streamlit run app.py` 时不会走包安装的路径，手动补上
_SRC = Path(__file__).resolve().parents[2]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import streamlit as st  # noqa: E402

st.set_page_config(
    page_title="舆情分析",
    page_icon="📡",
    layout="wide",
    initial_sidebar_state="expanded",
)

navigator = st.navigation(
    [
        # ⚠️ 这里**不要**写 url_path。Streamlit 规定「默认页的 url_path 恒为空字符串
        # （即根路径 /）」，`Page.url_path` 的实现就是 `"" if self._default else ...`，
        # 传进来的值会被**静默忽略** —— 不报错、不警告。
        #
        # 写了 url_path="dashboard" 的后果：看板端实际注册在 `/`，而 `/dashboard`
        # 这条路由根本不存在。用户访问 /dashboard 会看到 "Page not found ...
        # Running the app's main page."，再被回退到主页面（恰好就是看板端自己），
        # 表现为"报错后又刷新出来"。而 curl 该路径仍返回 200（SPA 任何路径都发
        # 同一个外壳），所以 HTTP 200 从来证明不了这条路由存在。
        st.Page("dashboard.py", title="看板端", icon="📊", default=True),  # → /
        st.Page("console.py", title="操作端", icon="🎛️", url_path="console"),  # → /console
    ]
)
navigator.run()
