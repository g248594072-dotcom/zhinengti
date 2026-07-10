# -*- coding: utf-8 -*-
"""
批量导入「已成交客户」聊天记录 → 写入 MySQL → 标记 status=已成交。

默认弹窗选文件；数据库目标在 import_deal_config.json（不提交 Git）。

每日自动拉取（SaleSmartly API）：
    python fetch_deal_daily.py
    建议 cron：0 2 * * * （在 daily_job.py 03:00 心理学习之前）

用法：
    cd 智能体
    python import_deal_customers.py

    python import_deal_customers.py --cli path/
    python import_deal_customers.py --cli --dry-run
    python import_deal_customers.py --cli --analyze --limit 10
"""

from __future__ import annotations

import argparse
import glob
import os
import sys

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

from deal_import_core import (
    execute_deal_import,
    get_mysql_target_label,
    load_import_config,
    save_import_config,
)


def _gui_available() -> bool:
    try:
        import tkinter as tk  # noqa: F401
        return True
    except Exception:
        return False


def pick_files_gui(cfg: dict) -> list[str]:
    import tkinter as tk
    from tkinter import filedialog, messagebox

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)

    target = get_mysql_target_label(cfg)
    messagebox.showinfo(
        "导入已成交客户",
        f"即将导入到 MySQL：\n{target}\n\n"
        "请在下一步选择 .xlsx 聊天记录（可多选）。\n"
        "数据库账号密码在 .env 中配置。",
    )

    initial = (cfg.get("import_last_dir") or "").strip()
    if initial and not os.path.isdir(initial):
        initial = os.path.dirname(initial) if os.path.isfile(initial) else _APP_DIR

    files = filedialog.askopenfilenames(
        title="选择已成交客户聊天记录（可多选）",
        initialdir=initial or _APP_DIR,
        filetypes=[("Excel 文件", "*.xlsx"), ("所有文件", "*.*")],
    )
    root.destroy()

    if not files:
        return []

    cfg["import_last_dir"] = os.path.dirname(files[0])
    save_import_config(cfg)
    return list(files)


def ask_run_analyze_gui(cfg: dict) -> tuple[bool, int]:
    import tkinter as tk
    from tkinter import messagebox, simpledialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)

    default_limit = int(cfg.get("default_analyze_limit") or 20)
    yes = messagebox.askyesno(
        "成交心理学习",
        "导入并标记「已成交」后，是否立刻调用 AI 做成交心理学习？\n\n"
        "· 选「是」：本次最多分析若干客户（耗 API）\n"
        "· 选「否」：仅入库，凌晨 daily_job 会自动学习",
    )
    limit = default_limit
    if yes:
        limit = simpledialog.askinteger(
            "学习数量上限",
            f"本次最多分析几个客户？（默认 {default_limit}）",
            initialvalue=default_limit,
            minvalue=1,
            maxvalue=200,
            parent=root,
        )
        if limit is None:
            limit = default_limit
        cfg["default_analyze_limit"] = limit
        save_import_config(cfg)

    root.destroy()
    return yes, limit


def show_done_gui(result: dict) -> None:
    import tkinter as tk
    from tkinter import messagebox

    summary = result.get("summary") or {}
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    msg = (
        f"导入目标：{result.get('target')}\n\n"
        f"新增客户：{summary.get('customers_created', 0)}\n"
        f"新增消息：{summary.get('messages_inserted', 0)}\n"
        f"已跳过：{summary.get('unchanged_sessions', 0)}\n"
        f"标记已成交：{summary.get('marked_deal', 0)} 个\n"
    )
    if result.get("learn_result"):
        msg += (
            f"\n心理学习：待分析 {summary.get('learn_total', 0)}，"
            f"成功 {summary.get('learn_success', 0)}，"
            f"失败 {summary.get('learn_failed', 0)}"
        )
    else:
        msg += "\n未执行心理学习（可等凌晨 daily_job）"
    if summary.get("errors"):
        msg += f"\n\n有 {len(summary['errors'])} 条警告，详见控制台。"
    messagebox.showinfo("导入完成", msg)
    root.destroy()


def show_error_gui(title: str, msg: str) -> None:
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    messagebox.showerror(title, msg)
    root.destroy()


def collect_xlsx_paths(paths: list[str]) -> list[str]:
    files: list[str] = []
    for p in paths:
        p = os.path.abspath(p)
        if os.path.isdir(p):
            files.extend(sorted(glob.glob(os.path.join(p, "*.xlsx"))))
        elif os.path.isfile(p) and p.lower().endswith(".xlsx"):
            files.append(p)
    seen: set[str] = set()
    out: list[str] = []
    for f in files:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def run_import(
    files: list[str],
    *,
    dry_run: bool = False,
    run_analyze: bool = False,
    analyze_limit: int = 20,
    use_gui: bool = False,
) -> int:
    if not files:
        print("未选择任何文件。")
        return 1

    print(f"导入目标：{get_mysql_target_label()}")
    print(f"共 {len(files)} 个 Excel 文件")

    result = execute_deal_import(
        files=files,
        dry_run=dry_run,
        run_analyze=run_analyze,
        analyze_limit=analyze_limit,
    )

    if result.get("error") and not result.get("ok"):
        print(result["error"])
        if use_gui:
            show_error_gui("导入失败", result["error"])
        return 1

    print(f"合计解析 {result['summary'].get('session_count', len(result.get('sessions') or []))} 通会话")

    if dry_run:
        for s in (result.get("sessions") or [])[:5]:
            print(
                f"  会话ID={s.get('会话ID')} | 联系人={s.get('联系人')} | "
                f"对话 {len(s.get('对话') or '')} 字"
            )
        print("\n(dry-run，未写入数据库)")
        return 0

    summary = result["summary"]
    settlement = summary.get("settlement") or {}
    print("\n=== 导入结算 ===")
    print(f"  解析会话：{settlement.get('total_sessions', summary.get('session_count', 0))}")
    print(f"  成功入库：{settlement.get('success_count', 0)}")
    print(f"  跳过：{settlement.get('skipped_count', 0)}")
    print(f"  失败：{settlement.get('failed_count', 0)}")
    print(f"  新增消息：{summary.get('messages_inserted', 0)}")
    print(f"  标记已成交：{summary.get('marked_deal', 0)} 个客户")
    for reason, cnt in (settlement.get("skip_by_reason") or {}).items():
        print(f"  跳过原因 · {reason}：{cnt}")
    for reason, cnt in (settlement.get("fail_by_reason") or {}).items():
        print(f"  失败原因 · {reason}：{cnt}")

    if result.get("error"):
        print(result["error"])
        if use_gui:
            show_error_gui("部分失败", result["error"])
        return 1

    if use_gui:
        show_done_gui(result)
    print("\n完成。")
    return 0


def main():
    parser = argparse.ArgumentParser(description="导入已成交客户 Excel 到 MySQL")
    parser.add_argument("paths", nargs="*", help="xlsx 或目录（仅 --cli 模式）")
    parser.add_argument("--cli", action="store_true", help="无弹窗，命令行模式")
    parser.add_argument("--dry-run", action="store_true", help="只预览不写库")
    parser.add_argument("--analyze", action="store_true", help="导入后立刻心理学习（--cli）")
    parser.add_argument("--limit", type=int, default=None, help="心理学习上限")
    args = parser.parse_args()

    import_cfg = load_import_config()
    analyze_limit = args.limit or int(import_cfg.get("default_analyze_limit") or 20)

    if args.cli:
        paths = args.paths
        if not paths:
            print("CLI 模式请传入 xlsx 文件或目录")
            sys.exit(1)
        files = collect_xlsx_paths(paths)
        sys.exit(run_import(
            files,
            dry_run=args.dry_run,
            run_analyze=args.analyze,
            analyze_limit=analyze_limit,
            use_gui=False,
        ))

    if not _gui_available():
        print("当前环境无法弹窗，请使用 --cli 模式。")
        sys.exit(1)

    if args.paths:
        files = collect_xlsx_paths(args.paths)
        run_analyze = args.analyze
    else:
        files = pick_files_gui(import_cfg)
        if not files:
            print("已取消。")
            sys.exit(0)
        if args.dry_run:
            run_analyze = False
        elif args.analyze:
            run_analyze = True
        else:
            run_analyze, analyze_limit = ask_run_analyze_gui(import_cfg)

    sys.exit(run_import(
        files,
        dry_run=args.dry_run,
        run_analyze=run_analyze,
        analyze_limit=analyze_limit,
        use_gui=True,
    ))


if __name__ == "__main__":
    main()
