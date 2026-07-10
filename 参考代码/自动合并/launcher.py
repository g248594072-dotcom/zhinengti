# -*- coding: utf-8 -*-
"""打包版启动入口：先开筛选窗，再加载重模块。"""

from __future__ import annotations

import importlib.util
import os
import sys
import traceback


def _resource_dir() -> str:
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    return os.path.dirname(os.path.abspath(__file__))


def app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _load_new72_module():
    res = _resource_dir()
    base = app_dir()
    for path in (res, base):
        if path not in sys.path:
            sys.path.insert(0, path)

    script = os.path.join(res, "new7.2.py")
    if not os.path.isfile(script):
        script = os.path.join(base, "new7.2.py")
    if not os.path.isfile(script):
        raise RuntimeError(f"找不到主程序：new7.2.py（已搜索 {res} 与 {base}）")

    spec = importlib.util.spec_from_file_location("merge_app", script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载：{script}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run() -> None:
    base = app_dir()
    res = _resource_dir()
    os.chdir(base)
    for path in (res, base):
        if path not in sys.path:
            sys.path.insert(0, path)

    from filter_window import FilterWindow

    filter_win = FilterWindow()
    filter_win.mainloop()

    if filter_win.selected is None:
        return

    mod = _load_new72_module()
    mod.run_with_selection(filter_win.selected)


def _show_fatal_error(exc: BaseException) -> None:
    log_path = os.path.join(app_dir(), "startup_error.log")
    text = traceback.format_exc()
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(text)
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "启动失败",
            f"程序启动出错，详情已写入：\n{log_path}\n\n{text}",
        )
        root.destroy()
    except Exception:
        pass


if __name__ == "__main__":
    try:
        _run()
    except Exception as e:
        _show_fatal_error(e)
