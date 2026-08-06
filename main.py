#!/usr/bin/env python3
"""澳门一户通羽毛球抢场工具（骨架）入口。"""

from __future__ import annotations

import argparse
import sys

from courtbot.browser import BrowserManager
from courtbot.config import load_config
from courtbot.logger import setup_logging
from courtbot.runner import Runner


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="澳门一户通羽毛球抢场工具（骨架）")
    ap.add_argument("--config", default="config.yaml", help="配置文件路径")
    ap.add_argument(
        "--fresh",
        action="store_true",
        help="使用全新的浏览器 profile（不受旧登录会话影响，推荐登录排查时使用）",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login", help="仅登录一户通并保存会话")
    sub.add_parser("discover", help="打开浏览器逐步保存页面结构（集成选择器用）")
    sub.add_parser("snap", help="手动操作浏览器时，输入 1 保存快照、0 退出")

    p_check = sub.add_parser("check", help="查询目标日期场次余量")
    p_check.add_argument("--date", default=None, help="指定日期 YYYY-MM-DD（默认取放场日+3天）")

    p_book = sub.add_parser("book", help="等待放场时间并抢场")
    p_book.add_argument("--dry-run", action="store_true", help="只等待放场并查询余量，不下单")

    p_cancel = sub.add_parser("cancel", help="取消待付款订单（不传订单号则取消当前 Lock 订单）")
    p_cancel.add_argument("order_id", nargs="?", default=None, help="订单号，如 IDOB260806160844S5R")

    sub.add_parser("choose", help="交互式选择场馆/日期/时段并保存")
    return ap


def main() -> int:
    args = build_parser().parse_args()
    cfg = load_config(args.config)
    if args.fresh:
        cfg.browser.fresh_profile = True
    log = setup_logging(cfg.state_dir)
    runner = Runner(cfg)

    try:
        if args.cmd == "login":
            with BrowserManager(cfg) as bm:
                from courtbot import login

                login.login(bm.driver, cfg)
                log.info("登录成功，会话保存在浏览器 profile 中")
        elif args.cmd == "discover":
            runner.discover()
        elif args.cmd == "snap":
            runner.snap()
        elif args.cmd == "check":
            runner.check(day=args.date)
        elif args.cmd == "book":
            runner.book(dry_run=args.dry_run)
        elif args.cmd == "cancel":
            runner.cancel_order(order_id=args.order_id)
        elif args.cmd == "choose":
            runner.choose()
        return 0
    except NotImplementedError as exc:
        log.error("尚未完成集成: %s", exc)
        return 2
    except Exception as exc:  # noqa: BLE001
        log.exception("执行失败: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
