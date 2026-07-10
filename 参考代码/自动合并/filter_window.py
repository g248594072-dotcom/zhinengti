# -*- coding: utf-8 -*-
"""筛选弹窗：访客标签（日期）、分组客服"""

from __future__ import annotations

import threading
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime
from tkinter import messagebox, ttk
from typing import Dict, List, Optional, Tuple

from fetch_salesmartly import (
    build_member_groups,
    fetch_filter_metadata,
    member_display_name,
    sort_member_group_names,
)
from salesmartly_client import ConfigError, SaleSmartlyClient, load_config, save_config


@dataclass
class FilterSelection:
    agent_ids: List[int]
    agent_names: List[str]
    tag_names: List[str]
    member_id_to_name: Dict[int, str]
    use_api: bool = True


def _bind_mousewheel(canvas: tk.Canvas) -> None:
    def _on_mousewheel(event):
        if event.delta:
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        elif event.num == 4:
            canvas.yview_scroll(-3, "units")
        elif event.num == 5:
            canvas.yview_scroll(3, "units")

    def _bind(_event=None):
        canvas.bind_all("<MouseWheel>", _on_mousewheel)
        canvas.bind_all("<Button-4>", _on_mousewheel)
        canvas.bind_all("<Button-5>", _on_mousewheel)

    def _unbind(_event=None):
        canvas.unbind_all("<MouseWheel>")
        canvas.unbind_all("<Button-4>")
        canvas.unbind_all("<Button-5>")

    canvas.bind("<Enter>", _bind)
    canvas.bind("<Leave>", _unbind)


def _make_scroll_area(parent: tk.Widget, height: Optional[int] = None) -> tuple[tk.Frame, tk.Frame]:
    outer = tk.Frame(parent, bd=1, relief=tk.SUNKEN)
    canvas = tk.Canvas(outer, highlightthickness=0)
    if height is not None:
        canvas.configure(height=height)
    scroll = tk.Scrollbar(outer, orient="vertical", command=canvas.yview)
    inner = tk.Frame(canvas)

    window_id = canvas.create_window((0, 0), window=inner, anchor="nw")

    def _on_inner_configure(_event=None):
        canvas.configure(scrollregion=canvas.bbox("all"))

    def _on_canvas_configure(event):
        canvas.itemconfig(window_id, width=event.width)

    inner.bind("<Configure>", _on_inner_configure)
    canvas.bind("<Configure>", _on_canvas_configure)
    canvas.configure(yscrollcommand=scroll.set)
    _bind_mousewheel(canvas)

    canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    scroll.pack(side=tk.RIGHT, fill=tk.Y)
    return outer, inner


class ConfigSetupDialog(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.title("配置 API")
        self.geometry("460x220")
        self.resizable(False, False)
        self.result = False

        tk.Label(self, text="API Token：").grid(row=0, column=0, sticky="w", padx=12, pady=10)
        self.ent_token = tk.Entry(self, width=42, show="*")
        self.ent_token.grid(row=0, column=1, padx=8, pady=10)

        tk.Label(self, text="项目 ID：").grid(row=1, column=0, sticky="w", padx=12, pady=6)
        self.ent_project = tk.Entry(self, width=42)
        self.ent_project.grid(row=1, column=1, padx=8, pady=6)

        tk.Label(
            self,
            text="在项目设置 → 企业开发设置 → API Token 中获取",
            fg="gray",
        ).grid(row=2, column=0, columnspan=2, sticky="w", padx=12)

        btn_frame = tk.Frame(self)
        btn_frame.grid(row=3, column=0, columnspan=2, pady=16)
        tk.Button(btn_frame, text="保存并继续", command=self._save).pack(side=tk.LEFT, padx=8)
        tk.Button(btn_frame, text="取消", command=self.destroy).pack(side=tk.LEFT, padx=8)

        self.transient(parent)
        self.grab_set()

    def _save(self):
        token = self.ent_token.get().strip()
        project = self.ent_project.get().strip()
        if not token or not project:
            messagebox.showwarning("提示", "请填写 API Token 和项目 ID。", parent=self)
            return
        try:
            save_config(token, project)
            self.result = True
            self.destroy()
        except Exception as e:
            messagebox.showerror("保存失败", str(e), parent=self)


class ProgressDialog(tk.Toplevel):
    """拉取进度弹窗：显示总量/已完成、进度条；关闭即取消。"""

    def __init__(self, parent=None, title="正在拉取数据"):
        super().__init__(parent)
        self.title(title)
        self.geometry("480x200")
        self.resizable(False, False)
        self._cancelled = False
        self._on_cancel_callback = None

        title_bar = tk.Frame(self)
        title_bar.pack(fill=tk.X, padx=8, pady=(8, 0))

        tk.Label(title_bar, text=title, anchor="w", font=("", 9, "bold")).pack(
            side=tk.LEFT, fill=tk.X, expand=True
        )
        tk.Button(
            title_bar,
            text="—",
            width=3,
            relief=tk.GROOVE,
            command=self._minimize,
        ).pack(side=tk.RIGHT)

        self.stage_label = tk.Label(self, text="准备中…", anchor="w", wraplength=440, justify="left")
        self.stage_label.pack(fill=tk.X, padx=16, pady=(12, 4))

        self.count_label = tk.Label(self, text="0 / 0", anchor="w", font=("", 10, "bold"))
        self.count_label.pack(fill=tk.X, padx=16, pady=(0, 6))

        self.progress = ttk.Progressbar(self, mode="determinate", maximum=100, value=0)
        self.progress.pack(fill=tk.X, padx=16, pady=(0, 8))

        self.hint_label = tk.Label(self, text="关闭此窗口将终止拉取", fg="gray", anchor="w")
        self.hint_label.pack(fill=tk.X, padx=16, pady=(0, 12))

        self.protocol("WM_DELETE_WINDOW", self._on_user_close)
        self.bind("<Map>", self._on_restore)
        try:
            self.attributes("-topmost", True)
        except Exception:
            pass
        self.update_idletasks()
        self._center()
        self.lift()
        self.focus_force()

    def _minimize(self):
        try:
            self.attributes("-topmost", False)
        except Exception:
            pass
        self.iconify()

    def _on_restore(self, _event=None):
        if str(self.state()) == "iconic":
            return
        try:
            self.attributes("-topmost", True)
        except Exception:
            pass
        self.lift()

    def _center(self):
        self.update_idletasks()
        w, h = 480, 200
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 2)
        self.geometry(f"{w}x{h}+{x}+{y}")

    def set_on_cancel(self, callback) -> None:
        self._on_cancel_callback = callback

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def _on_user_close(self):
        if self._cancelled:
            return
        self._cancelled = True
        self.stage_label.config(text="正在取消，请稍候…")
        self.hint_label.config(text="正在终止后台请求…", fg="#c0392b")
        if self._on_cancel_callback:
            try:
                self._on_cancel_callback()
            except Exception:
                pass

    def apply_snapshot(self, stage: str, current: int, total: int, detail: str = "") -> None:
        total = max(int(total), 1)
        current = min(max(int(current), 0), total)
        pct = int(current * 100 / total)
        self.stage_label.config(text=stage)
        self.count_label.config(text=f"已完成 {current} / 共 {total}  （{pct}%）")
        self.progress.configure(maximum=total, value=current)
        if detail:
            self.hint_label.config(text=detail, fg="gray")
        self.update_idletasks()

    def set_message(self, msg: str):
        """兼容旧接口。"""
        self.stage_label.config(text=msg)
        self.update_idletasks()


class FilterWindow(tk.Tk):
    TAG_COLUMNS = 5

    def __init__(self):
        super().__init__()
        self.title("每日数据生成")
        self.geometry("560x580")
        self.minsize(520, 500)
        self.selected: Optional[FilterSelection] = None
        self.use_file_mode = False

        self.client: Optional[SaleSmartlyClient] = None
        self.members: List[dict] = []
        self.member_id_to_name: Dict[int, str] = {}
        self.member_groups: Dict[str, List[Tuple[int, str]]] = {}
        self.time_tags: List[str] = []

        self.tag_vars: Dict[str, tk.BooleanVar] = {}
        self.agent_vars: Dict[int, tk.BooleanVar] = {}
        self.group_vars: Dict[str, tk.BooleanVar] = {}
        self.group_member_map: Dict[str, List[int]] = {}
        self.group_frames: Dict[str, tk.Frame] = {}
        self.group_expanded: Dict[str, tk.BooleanVar] = {}

        self._build_ui()
        self.after(100, self._bootstrap)

    def _build_ui(self):
        pad = {"padx": 16, "pady": 4}

        tag_block = tk.Frame(self)
        tag_block.pack(fill=tk.X, padx=16, pady=(14, 4))
        tag_head = tk.Frame(tag_block)
        tag_head.pack(fill=tk.X)
        tk.Label(tag_head, text="访客标签（日期·澳）", anchor="w").pack(side=tk.LEFT)
        tk.Label(tag_head, text="标签即时间，请至少选 1 个", fg="gray").pack(side=tk.RIGHT)
        self.tag_scroll_outer, self.tag_inner = _make_scroll_area(tag_block, height=110)
        self.tag_scroll_outer.pack(fill=tk.X, pady=(6, 0))

        agent_block = tk.Frame(self)
        agent_block.pack(fill=tk.BOTH, expand=True, **pad)
        agent_head = tk.Frame(agent_block)
        agent_head.pack(fill=tk.X)
        tk.Label(agent_head, text="接待客服（按分组，至少选 1 人）", anchor="w").pack(side=tk.LEFT)
        self.agent_scroll_outer, self.agent_inner = _make_scroll_area(agent_block)
        self.agent_scroll_outer.pack(fill=tk.BOTH, expand=True, pady=(6, 0))

        self.status_label = tk.Label(self, text="正在加载…", fg="gray", anchor="w")
        self.status_label.pack(fill=tk.X, padx=16, pady=(0, 4))

        bottom = tk.Frame(self)
        bottom.pack(fill=tk.X, padx=16, pady=12)
        tk.Button(bottom, text="全选客服", command=self._select_all_agents).pack(side=tk.LEFT)
        tk.Button(bottom, text="清空客服", command=self._clear_agents).pack(side=tk.LEFT, padx=8)
        tk.Button(bottom, text="生成报表", command=self._confirm).pack(side=tk.RIGHT)

        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self._bring_to_front()

    def _bring_to_front(self):
        self.update_idletasks()
        w = max(self.winfo_width(), 560)
        h = max(self.winfo_height(), 580)
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 2)
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.deiconify()
        self.lift()
        self.attributes("-topmost", True)
        self.after(300, lambda: self.attributes("-topmost", False))
        try:
            self.focus_force()
        except Exception:
            pass

    def _on_cancel(self):
        self.selected = None
        self.destroy()

    def _bootstrap(self):
        try:
            config = load_config()
        except ConfigError:
            dlg = ConfigSetupDialog(self)
            self.wait_window(dlg)
            if not dlg.result:
                self.destroy()
                return
            try:
                config = load_config()
            except ConfigError as e:
                messagebox.showerror("配置错误", str(e), parent=self)
                self.destroy()
                return

        self.client = SaleSmartlyClient(config)
        self.status_label.config(text="正在加载访客标签和客服分组…", fg="gray")
        self.update_idletasks()
        threading.Thread(target=self._load_metadata_worker, daemon=True).start()

    def _load_metadata_worker(self) -> None:
        try:
            members, time_tags = fetch_filter_metadata(self.client)
            self.after(0, lambda: self._apply_metadata(members, time_tags))
        except Exception as exc:
            self.after(0, lambda: self._on_load_failed(exc))

    def _on_load_failed(self, exc: Exception) -> None:
        if not self.winfo_exists():
            return
        messagebox.showerror("加载失败", f"无法从 API 获取数据：\n{exc}", parent=self)
        self.destroy()

    def _apply_metadata(self, members: List[dict], time_tags: List[str]) -> None:
        if not self.winfo_exists():
            return

        self.members = members
        self.time_tags = time_tags
        self.member_groups = build_member_groups(self.members)

        self.member_id_to_name = {}
        for m in self.members:
            uid = m.get("sys_user_id")
            if uid is None:
                continue
            try:
                self.member_id_to_name[int(uid)] = member_display_name(m)
            except (TypeError, ValueError):
                continue

        self._render_time_tags()
        self._render_agent_groups()
        self.status_label.config(
            text=f"访客标签 {len(self.time_tags)} 个 · 客服 {len(self.members)} 人 · 分组 {len(self.member_groups)} 个",
            fg="black",
        )

    def _render_time_tags(self):
        for w in self.tag_inner.winfo_children():
            w.destroy()
        self.tag_vars.clear()

        if not self.time_tags:
            tk.Label(
                self.tag_inner,
                text="未找到「澳大利亚 - 日期」时间标签，请检查 SaleSmartly 标签配置",
                fg="gray",
                wraplength=480,
                justify="left",
            ).pack(padx=8, pady=8, anchor="w")
            return

        today_tag = f"{datetime.now().month}..{datetime.now().day}"
        for idx, tag in enumerate(self.time_tags):
            default_on = tag == today_tag
            var = tk.BooleanVar(value=default_on)
            self.tag_vars[tag] = var
            row, col = divmod(idx, self.TAG_COLUMNS)
            tk.Checkbutton(
                self.tag_inner,
                text=tag,
                variable=var,
                width=8,
                anchor="w",
            ).grid(row=row, column=col, sticky="w", padx=6, pady=3)

    def _render_agent_groups(self):
        for w in self.agent_inner.winfo_children():
            w.destroy()
        self.agent_vars.clear()
        self.group_vars.clear()
        self.group_member_map.clear()
        self.group_frames.clear()
        self.group_expanded.clear()

        if not self.member_groups:
            tk.Label(self.agent_inner, text="（未获取到客服分组）", fg="gray").pack(padx=8, pady=8)
            return

        for group_name in sort_member_group_names(self.member_groups):
            members = self.member_groups[group_name]
            member_ids = [uid for uid, _ in members]
            self.group_member_map[group_name] = member_ids

            block = tk.Frame(self.agent_inner)
            block.pack(fill=tk.X, padx=4, pady=(4, 2))

            header = tk.Frame(block)
            header.pack(fill=tk.X)

            expanded = tk.BooleanVar(value=False)
            self.group_expanded[group_name] = expanded

            members_frame = tk.Frame(block)

            def _toggle_expand(gn=group_name, mf=members_frame, exp=expanded):
                if exp.get():
                    mf.pack(fill=tk.X, padx=18)
                else:
                    mf.pack_forget()

            def _flip_expand(gn=group_name, exp=expanded, toggle=_toggle_expand):
                exp.set(not exp.get())
                toggle()

            expand_btn = tk.Button(
                header,
                text="▶",
                width=2,
                relief=tk.FLAT,
                command=_flip_expand,
            )
            expand_btn.pack(side=tk.LEFT)

            def _sync_expand_icon(*_args, exp=expanded, btn=expand_btn):
                btn.config(text="▼" if exp.get() else "▶")

            expanded.trace_add("write", _sync_expand_icon)

            group_var = tk.BooleanVar(value=False)
            self.group_vars[group_name] = group_var

            def _on_group_toggle(*_args, ids=member_ids, gv=group_var):
                val = gv.get()
                for uid in ids:
                    if uid in self.agent_vars:
                        self.agent_vars[uid].set(val)

            group_var.trace_add("write", _on_group_toggle)

            tk.Checkbutton(
                header,
                text=group_name,
                variable=group_var,
                font=("", 9, "bold"),
                anchor="w",
            ).pack(side=tk.LEFT, fill=tk.X, expand=True)

            self.group_frames[group_name] = members_frame

            for uid, name in members:
                if uid not in self.agent_vars:
                    self.agent_vars[uid] = tk.BooleanVar(value=False)
                tk.Checkbutton(
                    members_frame,
                    text=name,
                    variable=self.agent_vars[uid],
                    anchor="w",
                ).pack(fill=tk.X, pady=1)

    def _select_all_agents(self):
        for var in self.agent_vars.values():
            var.set(True)
        for var in self.group_vars.values():
            var.set(True)

    def _clear_agents(self):
        for var in self.agent_vars.values():
            var.set(False)
        for var in self.group_vars.values():
            var.set(False)

    def _selected_tag_names(self) -> List[str]:
        return [name for name, var in self.tag_vars.items() if var.get()]

    def _confirm(self):
        agent_ids = [aid for aid, var in self.agent_vars.items() if var.get()]
        if not agent_ids:
            messagebox.showwarning("提示", "请至少选择 1 位接待客服。", parent=self)
            return

        tag_names = self._selected_tag_names()
        if not tag_names:
            messagebox.showwarning("提示", "请至少选择 1 个访客标签（标签即日期）。", parent=self)
            return

        agent_names = [self.member_id_to_name.get(aid, str(aid)) for aid in agent_ids]

        self.selected = FilterSelection(
            agent_ids=agent_ids,
            agent_names=agent_names,
            tag_names=tag_names,
            member_id_to_name=dict(self.member_id_to_name),
            use_api=True,
        )
        self.destroy()
