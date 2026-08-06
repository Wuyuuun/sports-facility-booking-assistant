#!/usr/bin/env python3
"""抢票助手 网页版 —— 纯鼠标操作（本地 HTTP 服务 + 浏览器界面）。

双击「抢票助手.command」启动：自动打开默认浏览器，所有操作都是点按钮/下拉框。
"""

from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import threading
import time
import webbrowser
from datetime import datetime, timedelta, time as dtime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(ROOT, ".venv", "bin", "python")
MAIN = os.path.join(ROOT, "main.py")
SELECTION_FILE = os.path.join(ROOT, "state", "selection.json")
PAY_URL_RE = re.compile(r"https://aas\.bocmacau\.com[^\s\"']*")
GUI_STATE_FILE = os.path.join(ROOT, "state", "gui.json")
SCHEDULE_FILE = os.path.join(ROOT, "state", "schedule.json")
SERVER = None


PAGE = """<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<title>抢票助手</title>
<style>
  body { font-family: -apple-system, "PingFang TC", sans-serif; margin: 20px; background:#f5f6f8; }
  h1 { font-size: 20px; margin: 0 0 10px; }
  .card { background:#fff; border-radius:10px; padding:14px 16px; margin-bottom:12px; box-shadow:0 1px 3px rgba(0,0,0,.08); }
  label { font-size:13px; color:#555; margin-right:4px; }
  select { font-size:14px; padding:4px 6px; border:1px solid #ccc; border-radius:6px; min-width:130px; }
  button { font-size:15px; padding:8px 18px; margin:4px 6px 4px 0; border:none; border-radius:8px; cursor:pointer; }
  .primary { background:#1a73e8; color:#fff; }
  .danger  { background:#d93025; color:#fff; }
  .gray    { background:#e4e6ea; color:#333; }
  .green   { background:#188038; color:#fff; }
  #sel { font-size:13px; color:#333; margin-top:8px; }
  #log { background:#111; color:#9fe09f; font:12px/1.5 Menlo,monospace; height:280px; overflow:auto; padding:10px; border-radius:8px; white-space:pre-wrap; }
  #status { font-size:13px; color:#666; margin-top:6px; }
</style></head><body>
<h1>🏸 抢票助手</h1>
<div class="card">
  <label>日期</label><select id="day"></select>
  <label>场地</label><select id="place"></select>
  <label>时段</label><select id="time"></select>
  <button class="gray" onclick="loadOptions()">刷新列表</button>
  <button class="primary" onclick="saveSel()">保存选择</button>
  <div id="sel">当前选择：未加载</div>
</div>
<div class="card">
  <button class="gray" onclick="run('check')">查余量</button>
  <button class="gray" onclick="run('rehearsal')">演练</button>
  <button class="primary" onclick="run('book')">抢场</button>
  <button class="danger" onclick="run('cancel')">取消订单</button>
  <button class="gray" onclick="stop()">停止</button>
  <button class="green" id="paybtn" onclick="openPay()" disabled>打开支付页</button>
  <button class="primary" onclick="schedule()">预定</button>
  <button class="gray" onclick="unschedule()">取消预定</button>
  <button class="gray" onclick="quit()">退出程序</button>
  <div id="status">就绪</div>
  <div id="sched">未预定</div>
</div>
<div class="card"><div id="log">等待日志…</div></div>
<script>
let seq = 0;
let opts = {dayRanges:[], places:[], times:[], selection:{}};

async function api(url, method, body) {
  const r = await fetch(url, {method: method || 'GET', headers:{'Content-Type':'application/json'},
                              body: body ? JSON.stringify(body) : undefined});
  return r.json();
}
function fill(sel, values) { sel.innerHTML = ''; values.forEach(v => { const o = document.createElement('option'); o.value = v; o.text = v; sel.appendChild(o); }); }

async function loadOptions() {
  const d = document.getElementById('day').value || '';
  opts = await api('/api/options' + (d ? '?day=' + encodeURIComponent(d) : ''));
  fill(document.getElementById('day'), opts.dayRanges || []);
  fill(document.getElementById('place'), (opts.places||[]).map(p => p.title));
  fill(document.getElementById('time'), (opts.times||[]).map(t => t.timeFrom + '-' + t.timeTo + (t.isCanBook ? '' : '（不可约）')));
  const sel = opts.selection || {};
  if (sel.day) document.getElementById('day').value = sel.day;
  if (sel.place_name) document.getElementById('place').value = sel.place_name;
  if (sel.time_label) {
    const t = document.getElementById('time');
    [...t.options].forEach(o => { if (o.value.startsWith(sel.time_label)) t.value = o.value; });
  }
  document.getElementById('sel').textContent = '当前选择：' + (sel.place_name||'') + '｜' + (sel.day||'') + '｜' + (sel.time_label||'');
}

async function saveSel() {
  const r = await api('/api/save', 'POST', {day: document.getElementById('day').value,
    placeTitle: document.getElementById('place').value,
    timeText: document.getElementById('time').value});
  document.getElementById('sel').textContent = '当前选择：' + document.getElementById('place').value +
    '｜' + document.getElementById('day').value + '｜' + document.getElementById('time').value;
  log('选择已保存\\n');
}

async function run(cmd) {
  const r = await api('/api/run', 'POST', {cmd, day: document.getElementById('day').value});
  if (!r.ok) log('已有任务运行中，请先点「停止」\\n');
  document.getElementById('status').textContent = '运行中…';
}
async function stop() { await api('/api/stop', 'POST', {}); }
async function openPay() {
  const r = await api('/api/payurl');
  if (r.payUrl) { window.open(r.payUrl, '_blank'); }
}
function log(t) {
  const el = document.getElementById('log');
  el.textContent += t;
  el.scrollTop = el.scrollHeight;
}
async function safeApi(url, method, body) {
  try {
    return await api(url, method, body);
  } catch (e) {
    log('请求失败：' + e + '\\n');
    return {};
  }
}
async function poll() {
  const r = await safeApi('/api/log?since=' + seq);
  seq = r.seq;
  (r.lines||[]).forEach(l => log(l));
  const st = await safeApi('/api/state');
  document.getElementById('status').textContent = st.running ? '运行中…' : '就绪';
  document.getElementById('paybtn').disabled = !st.payUrl;
  const sch = st.schedule || {};
  document.getElementById('sched').textContent = sch.day
    ? ('预定：' + sch.day + '｜放场 ' + (sch.release||'').replace('T',' ') + '｜' + (sch.started ? '已自动启动' : (sch.start||'').replace('T',' ') + ' 自动启动'))
    : '未预定';
  setTimeout(poll, 600);
}
async function quit() {
  if (confirm('确定退出抢票助手？')) { await safeApi('/api/quit', 'POST', {}); }
}
async function schedule() {
  const day = document.getElementById('day').value;
  const r = await safeApi('/api/schedule', 'POST', {day});
  log(r.ok ? ('已预定：' + day + '（放场前自动真抢）\\n') : '预定失败\\n');
}
async function unschedule() {
  await safeApi('/api/unschedule', 'POST', {});
  log('已取消预定\\n');
}
loadOptions();
poll();
</script></body></html>
"""


class Backend:
    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.log_lines: list[str] = []
        self.pay_url = ""
        self.last_options = {"dayRanges": [], "places": [], "times": [], "selection": {}}

    def append_log(self, text: str) -> None:
        with self.lock:
            self.log_lines.append(text)

    @property
    def seq(self) -> int:
        with self.lock:
            return len(self.log_lines)

    def run(self, args: list[str], env_extra: dict | None = None) -> bool:
        if self.proc and self.proc.poll() is None:
            return False
        cmd = [PY, MAIN] + args
        env = dict(os.environ)
        if env_extra:
            env.update(env_extra)
        self.append_log("$ " + " ".join(cmd) + "\n")
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
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
        return True

    def _reader(self, proc: subprocess.Popen) -> None:
        try:
            for line in proc.stdout:
                self.append_log(line)
                for m in PAY_URL_RE.finditer(line):
                    self.pay_url = m.group(0)
        finally:
            proc.wait()
            with self.lock:
                self.proc = None
            self.append_log("[任务结束]\n")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except Exception:
                pass
            self.append_log("已发送停止信号\n")

    def options(self, day: str = "") -> dict:
        args = ["options"] + (["--day", day] if day else [])
        env = dict(os.environ)
        try:
            p = subprocess.run(
                [PY, MAIN] + args,
                capture_output=True,
                text=True,
                cwd=ROOT,
                env=env,
                stdin=subprocess.DEVNULL,
                timeout=150,
            )
            raw = p.stdout
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}
        data = None
        for line in reversed(raw.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    data = json.loads(line)
                    break
                except Exception:
                    continue
        if isinstance(data, dict):
            self.last_options = data
            return data
        return {"error": raw[-500:] or "options 无输出"}

    def save_selection(self, day: str, place_title: str, time_text: str) -> bool:
        try:
            from courtbot.config import load_config

            cfg = load_config(os.path.join(ROOT, "config.yaml"))
            place = next((p for p in self.last_options.get("places", []) if p["title"] == place_title), {})
            tkey = next(
                (t["timeKey"] for t in self.last_options.get("times", [])
                 if f"{t['timeFrom']}-{t['timeTo']}" in time_text),
                "",
            )
            cur = {}
            if os.path.exists(SELECTION_FILE):
                try:
                    cur = json.load(open(SELECTION_FILE, encoding="utf-8"))
                except Exception:
                    cur = {}
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
            return True
        except Exception:  # noqa: BLE001
            return False


def _cfg():
    from courtbot.config import load_config

    return load_config(os.path.join(ROOT, "config.yaml"))


def _tz() -> ZoneInfo:
    return ZoneInfo(_cfg().timezone)


def compute_release(day: str) -> datetime:
    """目标日对应的放场时刻（目标日 - offset_days 的 release_time）。"""
    cfg = _cfg()
    hh, mm, ss = (int(x) for x in cfg.booking.release_time.split(":"))
    target = datetime.strptime(day, "%Y-%m-%d").date()
    return datetime.combine(
        target - timedelta(days=cfg.booking.offset_days),
        dtime(hh, mm, ss),
        tzinfo=_tz(),
    )


def load_schedule() -> dict | None:
    try:
        return json.load(open(SCHEDULE_FILE, encoding="utf-8"))
    except Exception:
        return None


def save_schedule(data: dict) -> None:
    os.makedirs(os.path.dirname(SCHEDULE_FILE), exist_ok=True)
    with open(SCHEDULE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def schedule_summary() -> dict:
    job = load_schedule()
    if not job or not job.get("day"):
        return {}
    release = compute_release(job["day"])
    warm = int(job.get("warm_minutes", 15))
    return {
        "day": job["day"],
        "release": release.isoformat(),
        "start": (release - timedelta(minutes=warm)).isoformat(),
        "started": bool(job.get("started")),
    }


def scheduler_loop(backend: Backend) -> None:
    """定时任务：到放场前 warm_minutes 分钟自动启动真实抢场（book）。"""
    while True:
        try:
            job = load_schedule()
            if job and job.get("day") and not job.get("started"):
                release = compute_release(job["day"])
                warm = timedelta(minutes=int(job.get("warm_minutes", 15)))
                now = datetime.now(_tz())
                if now >= release or now >= release - warm:
                    job["started"] = True
                    save_schedule(job)
                    when = (
                        "放场时间已过，立即"
                        if now >= release
                        else f"放场 {release.strftime('%H:%M:%S')} 前预热，自动"
                    )
                    backend.append_log(f"[预定] {when}启动真实抢场 {job['day']}\n")
                    if not backend.run(
                        ["book", "--day", job["day"]],
                        {"COURTBOT_OPEN_IN_DEFAULT": "1"},
                    ):
                        backend.append_log("[预定] 已有任务运行，本次自动启动跳过\n")
                        job["started"] = False
                        save_schedule(job)
        except Exception as exc:  # noqa: BLE001
            backend.append_log(f"[预定] 调度异常：{exc}\n")
        time.sleep(20)


class Handler(BaseHTTPRequestHandler):
    backend: Backend = Backend()

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: dict) -> None:
        self._send(200, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json")

    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        if u.path == "/":
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif u.path == "/api/state":
            self._json({
                "running": self.backend.proc is not None and self.backend.proc.poll() is None,
                "payUrl": self.backend.pay_url,
                "schedule": schedule_summary(),
            })
        elif u.path == "/api/log":
            q = parse_qs(u.query)
            try:
                since = int(q.get("since", ["0"])[0])
            except ValueError:
                since = 0
            with self.backend.lock:
                lines = self.backend.log_lines[since:]
            self._json({"seq": self.backend.seq, "lines": lines})
        elif u.path == "/api/payurl":
            self._json({"payUrl": self.backend.pay_url})
        elif u.path == "/api/options":
            q = parse_qs(u.query)
            self._json(self.backend.options(q.get("day", [""])[0]))
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length", 0) or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            body = {}
        if u.path == "/api/run":
            cmd = body.get("cmd", "")
            day = body.get("day", "")
            if cmd == "book":
                ok = self.backend.run(["book", "--day", day], {"COURTBOT_OPEN_IN_DEFAULT": "1"})
            elif cmd == "rehearsal":
                ok = self.backend.run(["book", "--rehearsal", "--day", day])
            elif cmd == "check":
                ok = self.backend.run(["check", "--date", day])
            elif cmd == "cancel":
                ok = self.backend.run(["cancel"])
            else:
                ok = False
            self._json({"ok": ok})
        elif u.path == "/api/stop":
            self.backend.stop()
            self._json({"ok": True})
        elif u.path == "/api/save":
            ok = self.backend.save_selection(
                body.get("day", ""), body.get("placeTitle", ""), body.get("timeText", "")
            )
            self._json({"ok": ok})
        elif u.path == "/api/quit":
            self.backend.stop()
            self._json({"ok": True})
            threading.Timer(0.3, shutdown_server).start()
        elif u.path == "/api/schedule":
            day = body.get("day", "")
            if day:
                save_schedule(
                    {
                        "day": day,
                        "warm_minutes": 15,
                        "started": False,
                        "created_at": datetime.now().isoformat(),
                    }
                )
                self._json({"ok": True, "schedule": schedule_summary()})
            else:
                self._json({"ok": False, "error": "缺少日期"})
        elif u.path == "/api/unschedule":
            try:
                os.remove(SCHEDULE_FILE)
            except Exception:
                pass
            self._json({"ok": True, "schedule": {}})
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, *args) -> None:  # 静默访问日志
        pass


def main() -> None:
    global SERVER
    existing = _existing_instance()
    if existing:
        webbrowser.open(f"http://127.0.0.1:{existing}/")
        print(f"抢票助手已在运行：http://127.0.0.1:{existing}/（直接打开页面）")
        return
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    SERVER = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    os.makedirs(os.path.dirname(GUI_STATE_FILE), exist_ok=True)
    with open(GUI_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"pid": os.getpid(), "port": port}, f)
    url = f"http://127.0.0.1:{port}/"
    print(f"抢票助手已启动：{url}")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    threading.Thread(target=scheduler_loop, args=(Handler.backend,), daemon=True).start()
    try:
        SERVER.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            os.remove(GUI_STATE_FILE)
        except Exception:
            pass


def _existing_instance() -> int | None:
    """返回已在运行的实例端口（pid 存活校验），否则 None。"""
    try:
        data = json.load(open(GUI_STATE_FILE, encoding="utf-8"))
        pid, port = data.get("pid"), data.get("port")
        if pid and port:
            os.kill(pid, 0)
            return int(port)
    except Exception:
        pass
    return None


def shutdown_server() -> None:
    if SERVER:
        try:
            SERVER.shutdown()
        except Exception:
            pass
        os._exit(0)


if __name__ == "__main__":
    main()
