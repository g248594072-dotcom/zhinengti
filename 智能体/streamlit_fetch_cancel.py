# -*- coding: utf-8 -*-
"""Streamlit 网页关闭时检测会话断连，用于取消 SaleSmartly 拉取。"""

from __future__ import annotations

import threading
from typing import Callable, Tuple

from fetch_cancel import CancelCheck


def make_streamlit_cancel_check() -> Tuple[CancelCheck, Callable[[], None]]:
    """
    返回 (cancel_check, request_cancel)。

    - 浏览器关闭标签页 / 断连后，cancel_check() 变为 True（线程安全）。
    - request_cancel() 供页面内「取消拉取」按钮使用。
    """
    from streamlit.runtime.runtime import Runtime
    from streamlit.runtime.scriptrunner_utils.script_run_context import get_script_run_ctx

    ctx = get_script_run_ctx()
    session_id = ctx.session_id if ctx else None
    lock = threading.Lock()
    user_cancelled = False

    def request_cancel() -> None:
        nonlocal user_cancelled
        with lock:
            user_cancelled = True

    def cancel_check() -> bool:
        with lock:
            if user_cancelled:
                return True
        if not session_id:
            return False
        try:
            runtime = Runtime.instance()
        except Exception:
            return False
        if runtime is None:
            return False
        return not runtime.is_active_session(session_id)

    return cancel_check, request_cancel
