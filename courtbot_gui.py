#!/usr/bin/env python3
"""抢票助手 GUI —— 纯鼠标操作版（Tkinter，无额外依赖）。

所有操作通过按钮/下拉框完成；后台用子进程运行 main.py 各命令，
日志实时显示；抢场时识别支付链接，可一键用默认浏览器打开扫码。
"""

from __future__ import annotations

import json
import os
import queue
import re
import signal
import subprocess
import threading
import webbrowser
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(ROOT, ".venv", "bin", "python")
MAIN = os.path.join(ROOT, "main.py")
SELECTION_FILE = os.path.join(ROOT, "state", "selection.json")

PAY_URL_RE = re.compile(r"https://aas\.bocmacau\.com[^\s\"']*")


class GuiApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("抢票助手 · 澳门一户通羽毛球场")
        root.geometry("780x580")
        root.minsize(660, 480)

        self.proc: subprocess.Popen | None = None
        self.pay_url: str | None = None
        self._last_raw = ""
        self.log_q: "queue.Queue[str | None]" = queue.Queue()
        self.opt = {"dayRanges": [], "places": [], "times": [], "selection": {}}

        self._build_ui()
        self.root.after(120, self._poll_log)
        self._refresh_options()

    # ---------------- UI ----------------
    def _build_ui(self) -> None:
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill="x")
        self.sel_label = ttk.Label(top, text="当前选择：未加载")
        self.sel_label.pack(side="left")

        mid = ttk.LabelFrame(self.root, text="选择目标", padding=8)
        mid.pack(fill="x", padx=8)
        ttk.Label(mid, text="日期").grid(row=0, column=0, sticky="w")
        self.day_var = tk.StringVar()
        self.day_cb = ttk.Combobox(mid, textvariable=self.day_var, width=12, state="readonly")
        self.day_cb.grid(row=0, column=1, padx=4)
        self.day_cb.bind("<<ComboboxSelected>>", lambda e: self._refresh_options())
        ttk.Label(mid, text="场地").grid(row=0, column=2, sticky="w")
        self.place_var = tk.StringVar()
        self.place_cb = ttk.Combobox(mid, textvariable=self.place_var, width=24, state="readonly")
        self.place_cb.grid(row=0, column=3, padx=4)
        ttk.Label(mid, text="时段").grid(row=0, column=4, sticky="w")
        self.time_var = tk.StringVar()
        self.time_cb = ttk.Combobox(mid, textvariable=self.time_var, width=24, state="readonly")
        self.time_cb.grid(row=0, column=5, padx=4)
        ttk.Button(mid, text="刷新列表", command=self._refresh_options).grid(row=0, column=6, padx=6)
        ttk.Button(mid, text="保存选择", command=self._save_selection).grid(row=0, column=7)

        acts = ttk.LabelFrame(self.root, text="操作", padding=8)
        acts.pack(fill="x", padx=8, pady=(8, 0))
        ttk.Button(acts, text="查余量", command=lambda: self._run(["check", "--date", self.day_var.get()])).pack(side="left", padx=4)
        ttk.Button(acts, text="演练", command=self._rehearsal).pack(side="left", padx=4)
        ttk.Button(acts, text="抢场", command=self._book).pack(side="left", padx=4)
        ttk.Button(acts, text="取消订单", command=lambda: self._run(["cancel"])).pack(side="left", padx=4)
        ttk.Button(acts, text="停止", command=self._stop).pack(side="left", padx=4)
        self.pay_btn = ttk.Button(acts, text="打开支付页", command=self._open_pay, state="disabled")
        self.pay_btn.pack(side="left", padx=4)

        logf = ttk.LabelFrame(self.root, text="日志", padding=4)
        logf.pack(fill="both", expand=True, padx=8, pady=8)
        self.log = scrolledtext.ScrolledText(logf, height=16, state="disabled", font=("Menlo", 10))
        self.log.pack(fill="both", expand=True)

        self.status = ttk.Label(self.root, text="就绪", anchor="w")
        self.status.pack(fill="x", padx=8, pady=(0, 6))

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- 选项加载 ----------------
    def _refresh_options(self) -> None:
        args = ["options"]
        if self.day_var.get():
            args += ["--day", self.day_var.get()]
        self._run(args, quiet=True, on_done=self._apply_options)

    def _apply_options(self, raw: str) -> None:
        # options 命令的 JSON 输出与日志混在同一流里，取最后一行以 { 开头的 JSON
        data = None
        for line in reversed(raw.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    data = json.loads(line)
                    break
                except Exception:
                    continue
        if not isinstance(data, dict):
            self._log("解析选项列表失败\n")
            return
        self.opt = data
        days = data.get("dayRanges") or []
        places = data.get("places") or []
        times = data.get("times") or []
        sel = data.get("selection") or {}

        self.day_cb["values"] = days
        self.place_cb["values"] = [p["title"] for p in places]
        self.time_cb["values"] = [
            f"{t['timeFrom']}-{t['timeTo']}" + ("" if t.get("isCanBook") else "（不可约）")
            for t in times
        ]
        if sel.get("day") in days:
            self.day_var.set(sel["day"])
        elif days:
            self.day_var.set(days[0])
        if sel.get("place_name") in self.place_cb["values"]:
            self.place_var.set(sel["place_name"])
        elif places:
            self.place_var.set(places[0]["title"])
        if sel.get("time_label") in self.time_cb["values"]:
            self.time_var.set(sel["time_label"])
        elif times:
            t0 = times[0]
            self.time_var.set(f"{t0['timeFrom']}-{t0['timeTo']}")
        self._update_sel_label()

    def _update_sel_label(self) -> None:
        self.sel_label.config(
            text=f"当前选择：{self.place_var.get()}｜{self.day_var.get()}｜{self.time_var.get()}"
        )

    def _save_selection(self) -> None:
        day = self.day_var.get()
        place_title = self.place_var.get()
        time_text = self.time_var.get()
        if not (day and place_title and time_text):
            messagebox.showwarning("提示", "请先选择日期/场地/时段")
            return
        place = next((p for p in self.opt["places"] if p["title"] == place_title), {})
        tkey = next(
            (t["timeKey"] for t in self.opt["times"]
             if f"{t['timeFrom']}-{t['timeTo']}" in time_text),
            "",
        )
        cur = {}
        if os.path.exists(SELECTION_FILE):
            try:
                cur = json.load(open(SELECTION_FILE, encoding="utf-8"))
            except Exception:
                cur = {}
        from courtbot.config import load_config

        cfg = load_config(os.path.join(ROOT, "config.yaml"))
        sel = {
            "place_id": place.get("id") or cfg.venue.place_id,
            "place_name": place_title,
            "day": day,
            "time_key": tkey or cfg.booking.time_key,
            "time_label": time_text,
            "venue_id": cur.get("venue_id") or cfg.venue.booking_venue_id,
            "sport_id": cur.get("sport_id") or cfg.venue.booking_sport_id,
        }
        os.makedirs(os.path.dirname(SELECTION_FILE), exist_ok=True)
        with open(SELECTION_FILE, "w", encoding="utf-8") as f:
            json.dump(sel, f, ensure_ascii=False, indent=2)
        self._update_sel_label()
        self._log(f"选择已保存：{place_title} {day} {time_text}\n")

    # ---------------- 命令执行 ----------------
    def _rehearsal(self) -> None:
        self._run(["book", "--rehearsal", "--day", self.day_var.get()])

    def _book(self) -> None:
        self._run(
            ["book", "--day", self.day_var.get()],
            env_extra={"COURTBOT_OPEN_IN_DEFAULT": "1"},
        )

    def _run(
        self,
        args: list[str],
        quiet: bool = False,
        on_done=None,
        env_extra: dict | None = None,
    ) -> None:
        if self.proc:
            self._log("已有任务运行中，请先点「停止」\n")
            return
        cmd = [PY, MAIN] + args
        env = dict(os.environ)
        if env_extra:
            env.update(env_extra)
        if not quiet:
            self._log("$ " + " ".join(cmd) + "\n")
        self.status.config(text="运行中…")
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            start_new_session=True,
            cwd=ROOT,
            env=env,
        )
        threading.Thread(target=self._reader, args=(self.proc, on_done), daemon=True).start()

    def _reader(self, proc: subprocess.Popen, on_done) -> None:
        try:
            for line in proc.stdout:
                self.log_q.put(line)
            proc.wait()
        finally:
            self.log_q.put(None)
            self.log_q.put(on_done)

    def _poll_log(self) -> None:
        try:
            while True:
                item = self.log_q.get_nowait()
                if item is None:
                    self._finish_task()
                    continue
                if callable(item):
                    raw = getattr(self, "_last_raw", "")
                    item(raw)
                    continue
                self._append_log(item)
                self._last_raw = (self._last_raw + item)[-20000:]
                for m in PAY_URL_RE.finditer(item):
                    self.pay_url = m.group(0)
                    self.pay_btn.config(state="normal")
                    self._append_log("→ 已识别支付链接，可点「打开支付页」扫码\n")
        except queue.Empty:
            pass
        self.root.after(120, self._poll_log)

    def _finish_task(self) -> None:
        self.proc = None
        self.status.config(text="就绪")

    def _append_log(self, text: str) -> None:
        self.log.config(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.config(state="disabled")

    def _log(self, text: str) -> None:
        self._append_log(text)

    def _stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except Exception:
                pass
            self._log("已发送停止信号\n")

    def _open_pay(self) -> None:
        if self.pay_url:
            webbrowser.open(self.pay_url)
            self._log(f"已用默认浏览器打开支付页：{self.pay_url}\n")

    def _on_close(self) -> None:
        self._stop()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    GuiApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
