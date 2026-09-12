"""可视化入口 —— 双页导航。

    看板端（dashboard.py）  只读：趋势、情感、主题、词云、传播、预警记录
    操作端（console.py）    可写：L1~L5 的触发与微调（采集/分析/规则/推送）

两者刻意分开（方案文档 §5.1 的存储/展示边界）：看板是长时间开着"随手看一眼"
的页面，操作端是改参数、触发动作的地方。混在一起，一个误点就可能重跑分析
或改掉预警规则。

启动：
    python -m Whochat.cli dashboard      # → http://localhost:6666
    # 直接打开某个入口：/dashboard 或 /console
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

st.navigation(
    [
        st.Page("dashboard.py", title="看板端", icon="📊", url_path="dashboard", default=True),
        st.Page("console.py", title="操作端", icon="🎛️", url_path="console"),
    ]
).run()
