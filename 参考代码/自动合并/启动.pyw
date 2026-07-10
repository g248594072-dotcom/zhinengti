# -*- coding: utf-8 -*-
"""Double-click this file to start the app (no black console window)."""

import os
import sys
import traceback


def _app_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _run():
    base = _app_dir()
    os.chdir(base)
    if base not in sys.path:
        sys.path.insert(0, base)

    import importlib.util

    script = os.path.join(base, "new7.2.py")
    spec = importlib.util.spec_from_file_location("merge_app", script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load: {script}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.main()


if __name__ == "__main__":
    try:
        _run()
    except Exception:
        log_path = os.path.join(_app_dir(), "startup_error.log")
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(traceback.format_exc())
        try:
            import tkinter as tk
            from tkinter import messagebox

            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                "启动失败",
                f"程序启动出错，详情已写入：\n{log_path}\n\n{traceback.format_exc()}",
            )
            root.destroy()
        except Exception:
            pass
