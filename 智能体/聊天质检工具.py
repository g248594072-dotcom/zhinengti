# -*- coding: utf-8 -*-
"""
大力丸 · 聊天记录质检工具（桌面版 Tkinter）
核心逻辑见 qc_core.py，与网页版 streamlit_app.py 共用。
"""

import os
import json
import time
import threading
import queue
import traceback

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import qc_core as core


class App:
    def __init__(self, root):
        self.root = root
        root.title("大力丸 · 聊天记录质检工具")
        root.geometry("760x560")
        self.cfg, self.config_warning = core.load_config_from_disk()
        self.log_q = queue.Queue()
        self.running = False
        self._build()
        self._poll_log()
        if self.config_warning:
            self.log("⚠ " + self.config_warning.replace("\n", " "))
            messagebox.showwarning("配置文件提示", self.config_warning)

    def load_config(self):
        self.cfg["base_url"] = self.e_url.get().strip()
        self.cfg["model"] = self.e_model.get().strip()
        self.cfg["api_key"] = self.e_key.get().strip()
        try:
            self.cfg["concurrency"] = int(self.e_conc.get().strip())
        except Exception:
            self.cfg["concurrency"] = core.DEFAULTS["concurrency"]
        return self.cfg

    def _missing_key_hint(self):
        example = os.path.basename(core.CONFIG_EXAMPLE_FILE)
        return (
            "请先填写 API Key。\n\n"
            f"若尚未创建配置文件，请复制「{example}」为 qc_config.json，"
            f"填入密钥后点「保存配置」。"
        )

    def save_config(self):
        self.load_config()
        try:
            with open(core.CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, ensure_ascii=False, indent=2)
            self.config_warning = None
            self.log("配置已保存到 " + core.CONFIG_FILE)
        except Exception as e:
            self.log("保存配置失败：" + str(e))
            messagebox.showerror("保存失败", f"无法写入配置文件：\n{core.CONFIG_FILE}\n\n{e}")

    def _build(self):
        pad = {"padx": 6, "pady": 4}
        frm = ttk.LabelFrame(self.root, text="① 接口设置（DeepSeek 等 OpenAI 兼容接口）")
        frm.pack(fill="x", **pad)

        ttk.Label(frm, text="Base URL").grid(row=0, column=0, sticky="e", **pad)
        self.e_url = ttk.Entry(frm, width=40)
        self.e_url.insert(0, self.cfg["base_url"])
        self.e_url.grid(row=0, column=1, sticky="w", **pad)

        ttk.Label(frm, text="模型").grid(row=0, column=2, sticky="e", **pad)
        self.e_model = ttk.Entry(frm, width=20)
        self.e_model.insert(0, self.cfg["model"])
        self.e_model.grid(row=0, column=3, sticky="w", **pad)

        ttk.Label(frm, text="API Key").grid(row=1, column=0, sticky="e", **pad)
        self.e_key = ttk.Entry(frm, width=40, show="*")
        self.e_key.insert(0, self.cfg["api_key"])
        self.e_key.grid(row=1, column=1, sticky="w", **pad)

        ttk.Label(frm, text="并发数").grid(row=1, column=2, sticky="e", **pad)
        self.e_conc = ttk.Entry(frm, width=20)
        self.e_conc.insert(0, str(self.cfg["concurrency"]))
        self.e_conc.grid(row=1, column=3, sticky="w", **pad)

        btns = ttk.Frame(frm)
        btns.grid(row=2, column=0, columnspan=4, sticky="w", **pad)
        ttk.Button(btns, text="保存配置", command=self.save_config).pack(side="left", padx=4)
        ttk.Button(btns, text="测试连接", command=self.test_conn).pack(side="left", padx=4)

        frm2 = ttk.LabelFrame(self.root, text="② 选择聊天记录并开始质检")
        frm2.pack(fill="x", **pad)
        self.btn_run = ttk.Button(frm2, text="选择 xlsx 表格并开始质检", command=self.on_run)
        self.btn_run.pack(side="left", padx=6, pady=6)
        self.lbl_status = ttk.Label(frm2, text="就绪")
        self.lbl_status.pack(side="left", padx=10)

        frm3 = ttk.LabelFrame(self.root, text="③ 运行日志")
        frm3.pack(fill="both", expand=True, **pad)
        self.txt = tk.Text(frm3, wrap="word", height=18)
        self.txt.pack(side="left", fill="both", expand=True, padx=4, pady=4)
        sb = ttk.Scrollbar(frm3, command=self.txt.yview)
        sb.pack(side="right", fill="y")
        self.txt.config(yscrollcommand=sb.set)

    def log(self, msg):
        self.log_q.put(msg)

    def _poll_log(self):
        try:
            while True:
                msg = self.log_q.get_nowait()
                self.txt.insert("end", time.strftime("[%H:%M:%S] ") + str(msg) + "\n")
                self.txt.see("end")
        except queue.Empty:
            pass
        self.root.after(200, self._poll_log)

    def test_conn(self):
        self.save_config()
        if not self.cfg["api_key"]:
            messagebox.showwarning("提示", self._missing_key_hint())
            return
        self.log("正在测试连接……")

        def worker():
            res = core.call_llm(self.cfg, '[2026-01-01 10:00:00] 客户 : "Hi, does it work?"')
            if res.get("_错误"):
                self.log("❌ 连接失败：" + str(res.get("_错误")))
            else:
                self.log("✅ 连接成功，模型可正常返回。")

        threading.Thread(target=worker, daemon=True).start()

    def on_run(self):
        if self.running:
            messagebox.showinfo("提示", "正在质检中，请稍候……")
            return
        self.save_config()
        if not self.cfg["api_key"]:
            messagebox.showwarning("提示", self._missing_key_hint())
            return
        files = filedialog.askopenfilenames(
            title="选择聊天记录表格（可多选）",
            filetypes=[("Excel 文件", "*.xlsx"), ("所有文件", "*.*")],
        )
        if not files:
            return
        self.running = True
        self.btn_run.config(state="disabled")
        threading.Thread(target=self._run_worker, args=(list(files),), daemon=True).start()

    def _run_worker(self, files):
        try:
            self.log(f"读取 {len(files)} 个文件……")
            sessions, diag, _ = core.load_sessions(files=files)

            for d in diag:
                if d.get("error"):
                    self.log(f"  ⚠ {d['file']} 读取失败：{d['error']}")
                    continue
                det = d.get("detected", {})
                self.log(f"  {d['file']}：{d['rows']} 行，列={d['columns']}")
                if not det.get("msg"):
                    self.log("    ⚠ 未找到“会话消息”列，已跳过此文件")
                else:
                    self.log(
                        f"    识别：消息={det.get('msg')} | ID={det.get('id')} | "
                        f"时间={det.get('time')} | 发送人={det.get('sender')}"
                    )

            if not sessions:
                cols_info = "\n".join(
                    f"· {d['file']}：{d.get('error') or d.get('columns')}" for d in diag
                )
                msg = (
                    "未解析出任何会话，已停止（不生成空报告）。\n\n"
                    "请选择含「会话消息 / 会话ID」等列的聊天记录表。\n\n"
                    f"本次检测到的列：\n{cols_info}"
                )
                self.log("❌ " + msg.replace("\n", " "))
                messagebox.showwarning("没有可质检的数据", msg)
                return

            tiers = [core.classify_tier(core.count_roles(s["对话"])[0]) for s in sessions]
            n_skip, n_light, n_full = tiers.count("skip"), tiers.count("light"), tiers.count("full")
            self.log(
                f"共解析出 {len(sessions)} 通会话。分档：跳过 {n_skip} / 轻量 {n_light} / 重点 {n_full}"
                f"（需调用 API {n_light + n_full} 次）。开始质检……"
            )

            def on_progress(done, total, row, tier):
                if tier == "skip":
                    flag = (
                        f"[规则筛选] 客户{row['客户发言数']}条/客服{row['客服发言数']}条 · "
                        f"{row.get('快速筛选', '')}"
                    )
                else:
                    tag, qual, err = row.get("结果标签", ""), row.get("是否合格", ""), row.get("_错误", "")
                    risk = row.get("风险等级", "")
                    flag = "❌" + str(err) if err else f"[{core.TIER_LABEL[tier]}] {tag} / {qual} / 风险{risk}"
                self.log(f"[{done}/{total}] {row['联系人']} → {flag}")
                self.lbl_status.config(text=f"进度 {done}/{total}")

            results = core.run_qc_batch(sessions, self.cfg, on_progress=on_progress)
            stats = core.compute_stats(results)
            self.log(
                f"质检完成：跳过 {stats['跳过数量']} / 轻量 {stats['轻量质检数量']} / "
                f"重点 {stats['完整质检数量']}，高风险 {stats['高风险数量']} 通。"
            )

            df_quality, df_low = core.split_report_results(results)
            out_path = os.path.join(
                os.path.dirname(files[0]),
                "质检报告_" + time.strftime("%Y%m%d_%H%M%S") + ".xlsx",
            )
            core.write_report_excel(results, out_path)
            self.log(
                f"✅ 完成！报告已保存：{out_path} "
                f"（{core.REPORT_SHEET_QUALITY} {len(df_quality)} 通 / "
                f"{core.REPORT_SHEET_LOW_INTENT} {len(df_low)} 通）"
            )
            try:
                os.startfile(out_path)
            except Exception:
                pass
            messagebox.showinfo("完成", "质检完成，报告已保存：\n" + out_path)
        except Exception as e:
            self.log("❌ 出错：" + str(e))
            self.log(traceback.format_exc())
            messagebox.showerror("出错", str(e))
        finally:
            self.running = False
            self.btn_run.config(state="normal")
            self.lbl_status.config(text="就绪")


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
