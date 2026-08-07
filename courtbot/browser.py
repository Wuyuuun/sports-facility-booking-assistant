from __future__ import annotations

import json
import logging
import re
import select
import sys
import time
from datetime import datetime
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

log = logging.getLogger("courtbot.browser")


class FlowRecorder:
    """通过 Chrome DevTools 协议记录请求头与响应体。

    用途：
    1. 捕获 booking.sport.gov.mo 所有 API 请求携带的 api-key 请求头；
    2. 捕获 order/add、payment/start 等关键请求的响应体，
       从而拿到 orderId、支付 URL 等（无需解析页面 DOM）。
    """

    def __init__(self, driver: webdriver.Chrome, state_dir: Path):
        self.driver = driver
        self.state_dir = state_dir
        self._req: dict[str, dict] = {}
        self._bodies: dict[str, str] = {}
        self._started = False

    def start(self) -> None:
        """开启网络记录（幂等，不会清空已记录内容）。"""
        if self._started:
            return
        self._started = True
        try:
            self.driver.execute_cdp_cmd("Network.enable", {})
        except Exception as exc:  # noqa: BLE001
            log.debug("Network.enable 失败（不影响后续）: %s", exc)

    def drain(self) -> None:
        try:
            for entry in self.driver.get_log("performance"):
                msg = json.loads(entry["message"])["message"]
                self._ingest(msg)
        except Exception as exc:  # noqa: BLE001
            log.debug("读取 performance 日志失败: %s", exc)

    def _ingest(self, msg: dict) -> None:
        method = msg.get("method", "")
        params = msg.get("params", {})
        if method == "Network.requestWillBeSent":
            req = params.get("request", {})
            wall = params.get("wallTime", 0)
            ts = wall / 1000 if wall else params.get("timestamp", time.time())
            self._req[params.get("requestId")] = {
                "url": req.get("url", ""),
                "method": req.get("method", ""),
                "headers": req.get("headers", {}),
                "postData": req.get("postData", ""),
                "ts": ts,
            }
        elif method == "Network.loadingFinished":
            rid = params.get("requestId")
            if rid in self._req:
                try:
                    body = self.driver.execute_cdp_cmd(
                        "Network.getResponseBody", {"requestId": rid}
                    )
                    self._bodies[rid] = body.get("body", "")
                except Exception:  # noqa: BLE001
                    pass
        elif method == "Network.responseReceived":
            rid = params.get("requestId")
            if rid in self._req:
                resp = params.get("response", {})
                self._req[rid]["status"] = resp.get("status", 0)
                self._req[rid]["statusText"] = resp.get("statusText", "")
                self._req[rid]["mimeType"] = resp.get("mimeType", "")
                self._req[rid]["respHeaders"] = resp.get("headers", {})

    def snapshot(self) -> list[dict]:
        self.drain()
        return list(self._req.values())

    def find_api_key(self) -> str:
        self.drain()
        key = ""
        for r in self._req.values():
            k = r["headers"].get("api-key") or r["headers"].get("Api-Key")
            if k:
                key = k
        # 取最后（最新）出现的 key：浏览器恢复旧页面时会先发出带旧 apicode 的请求，
        # 第一个 key 可能是已失效的，最新一个才是当前会话的。
        return key

    def wait_for_api_key(self, url_pattern: str, timeout: float = 10.0) -> str:
        """等待出现匹配 url_pattern 且带 api-key 的请求，返回该 key。

        预约 SPA 加载完成后才会请求 place/list 等接口并携带新 apicode 的 key，
        用它可避免先抓到旧页面残留的失效 key。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.drain()
            for r in self._req.values():
                if re.search(url_pattern, r["url"]):
                    k = r["headers"].get("api-key") or r["headers"].get("Api-Key")
                    if k:
                        return k
            time.sleep(0.2)
        return ""

    def wait_request(self, url_pattern: str, timeout: float = 60.0) -> dict:
        """等待出现匹配 url_pattern 的请求，返回其 url/method/headers/postData/body。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.drain()
            for rid, r in list(self._req.items()):
                if re.search(url_pattern, r["url"]):
                    out = dict(r)
                    out["body"] = self._bodies.get(rid, "")
                    return out
            time.sleep(0.1)
        raise TimeoutError(f"未捕获到匹配 {url_pattern!r} 的请求")

    def find_response(self, url_pattern: str) -> dict:
        """返回最近一条匹配 url_pattern 的请求（含响应体）；没有则返回空 dict。"""
        self.drain()
        match: dict = {}
        for rid, r in list(self._req.items()):
            if re.search(url_pattern, r["url"]):
                out = dict(r)
                out["body"] = self._bodies.get(rid, "")
                match = out
        return match

    def save_body(self, name: str, body: str) -> Path:
        ts = time.strftime("%Y%m%d-%H%M%S")
        out = self.state_dir / f"{ts}-{name}.json"
        out.write_text(body, encoding="utf-8")
        log.info("响应已保存: %s", out)
        return out

    def export_har(self) -> dict:
        """把已捕获的请求导出为 HAR 结构（含请求头/请求体/响应体）。"""
        self.drain()
        entries = []
        for rid, r in self._req.items():
            post = r.get("postData", "")
            body = self._bodies.get(rid, "")
            started = datetime.fromtimestamp(r.get("ts", time.time())).astimezone().isoformat()
            entries.append(
                {
                    "startedDateTime": started,
                    "time": 0,
                    "request": {
                        "method": r.get("method", ""),
                        "url": r.get("url", ""),
                        "httpVersion": "HTTP/1.1",
                        "headers": [
                            {"name": k, "value": str(v)}
                            for k, v in r.get("headers", {}).items()
                        ],
                        "queryString": [],
                        "cookies": [],
                        "headersSize": -1,
                        "bodySize": len(post),
                        **(
                            {
                                "postData": {
                                    "mimeType": "application/json;charset=UTF-8",
                                    "text": post,
                                }
                            }
                            if post
                            else {}
                        ),
                    },
                    "response": {
                        "status": r.get("status", 0),
                        "statusText": r.get("statusText", ""),
                        "httpVersion": "HTTP/1.1",
                        "headers": [
                            {"name": k, "value": str(v)}
                            for k, v in r.get("respHeaders", {}).items()
                        ],
                        "content": {
                            "size": len(body),
                            "mimeType": r.get("mimeType", ""),
                            "text": body,
                        },
                        "redirectURL": "",
                        "headersSize": -1,
                        "bodySize": len(body),
                    },
                    "cache": {},
                    "timings": {},
                    "serverIPAddress": "",
                }
            )
        entries.sort(key=lambda e: e["startedDateTime"])
        return {
            "log": {
                "version": "1.2",
                "creator": {"name": "courtbot", "version": "0.1.0"},
                "pages": [],
                "entries": entries,
            }
        }

    def save_har(self, label: str = "run") -> Path:
        """把本次运行的全部请求保存为 HAR 文件（每次运行自动调用）。"""
        self.drain()
        ts = time.strftime("%Y%m%d-%H%M%S")
        out = self.state_dir / f"{ts}-{label}.har"
        out.write_text(
            json.dumps(self.export_har(), ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        log.info("本次运行 HAR 已保存: %s（%d 条请求）", out, len(self._req))
        return out


class BrowserManager:
    def __init__(self, cfg):
        self.cfg = cfg
        self.driver: webdriver.Chrome | None = None
        self._recorder: FlowRecorder | None = None

    def start(self) -> webdriver.Chrome:
        opts = Options()
        if self.cfg.browser.headless:
            opts.add_argument("--headless=new")
        if self.cfg.browser.fresh_profile:
            ts = time.strftime("%Y%m%d-%H%M%S")
            profile = (self.cfg.state_dir / "profiles" / f"fresh-{ts}").resolve()
        else:
            profile = (self.cfg.state_dir / "chrome-profile").resolve()
        profile.mkdir(parents=True, exist_ok=True)
        opts.add_argument(f"--user-data-dir={profile}")
        opts.add_argument("--no-first-run")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})
        try:
            driver_binary = ChromeDriverManager().install()
        except Exception as exc:  # noqa: BLE001
            log.error("ChromeDriver 安装/查找失败: %s", exc)
            raise

        # profile 被残留进程占用时 Chrome 启动会失败；自动等待旧进程
        # 退出（如 keep_open 等待回车但窗口已关）后重试。
        last_exc: Exception | None = None
        for attempt in range(1, 4):
            try:
                self.driver = webdriver.Chrome(
                    service=Service(driver_binary), options=opts
                )
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt < 3:
                    log.warning(
                        "浏览器启动失败（第 %d/3 次）: %s。"
                        "若上一次运行的进程尚未退出（keep_open 等待回车/窗口未关/进程被杀），"
                        "正在等待其释放 profile（%s）后重试……",
                        attempt,
                        exc,
                        profile,
                    )
                    time.sleep(10)
        if self.driver is None:
            log.error(
                "浏览器启动失败: %s。若仍有残留进程占用 profile（%s），"
                "请关闭旧浏览器窗口，或结束旧的抢场进程后重试",
                last_exc,
                profile,
            )
            raise last_exc
        self.driver.set_window_size(1440, 960)
        captures_dir = self.cfg.state_dir / "captures"
        captures_dir.mkdir(parents=True, exist_ok=True)
        self._recorder = FlowRecorder(self.driver, captures_dir)
        self._recorder.start()
        return self.driver

    def recorder(self) -> FlowRecorder:
        return self._recorder

    def _browser_open(self) -> bool:
        """浏览器窗口是否仍打开；用户手动关闭窗口后返回 False。"""
        if not self.driver:
            return False
        try:
            return bool(self.driver.window_handles)
        except Exception:  # noqa: BLE001
            return False

    def _wait_keep_open(self) -> None:
        """keep_open：等待人工完成查看/支付。

        交互式终端：按回车立即结束；用户直接关闭浏览器窗口也会自动结束，
        避免进程一直占着 profile，导致下一次启动报错。
        非交互（GUI 子进程 stdin=DEVNULL）：维持原行为立即结束，不阻塞任务。
        """
        log.info(
            "keep_open=true：浏览器窗口保持打开。完成查看/支付后"
            "关闭浏览器窗口即自动结束进程，或回到本终端按回车结束"
        )
        stdin = sys.stdin
        interactive = bool(stdin) and not stdin.closed and stdin.isatty()
        if not interactive:
            return
        while True:
            try:
                ready, _, _ = select.select([stdin], [], [], 0.5)
            except (OSError, ValueError):
                return
            if ready:
                try:
                    stdin.readline()
                except Exception:  # noqa: BLE001
                    pass
                return
            if not self._browser_open():
                log.info("检测到浏览器窗口已关闭，自动结束进程")
                return

    def stop(self) -> None:
        if self.driver:
            if self._recorder:
                try:
                    self._recorder.save_har("run")
                except Exception:  # noqa: BLE001
                    log.debug("保存运行 HAR 失败", exc_info=True)
            if self.cfg.browser.keep_open:
                try:
                    self._wait_keep_open()
                except KeyboardInterrupt:
                    log.info("收到中断，结束 keep_open 等待")
            try:
                self.driver.quit()
            except Exception:  # noqa: BLE001
                pass
            self.driver = None
            self._recorder = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
