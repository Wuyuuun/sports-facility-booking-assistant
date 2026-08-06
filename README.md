# 澳门一户通 · 羽毛球抢场工具

针对澳门一户通「场地设施预约」服务编写的自动化抢场工具。目标：**奥林匹克运动场三号羽毛球场**，提前 3 天早上 8:00 放场，抢到后中银智慧付（MPay）扫码支付（支付环节始终手动）。

> 仅供个人低频使用。自动化访问政府网站存在使用条款与风控风险，请自行评估。

## 当前进度（2026-08-05 收尾）

- ✅ 登录/会话链路跑通：OAuth 授权入口 → venue JWT（`Authorization`）→ booking `api-key`
- ✅ 场馆/场次查询跑通：`place/list`、`setting/init`、`open_time` 余量解析
- ✅ 抢场点击对准真实页面：日期 → 场地 → 时段 → 勾选条款 → 「加入待付款清單」
- ✅ 已实际走到支付页（订单创建、短信验证码、二维码页面均验证过）
- ✅ 已确认：`order/add` 订单号=`data.number`；`payment/start` 支付 URL=`data.paymentLink`；滑块在模板正常加载时必须拖动（不拖则 `trerror` 错误票据、不建单）；BOC 状态接口 `getOrderStatus.do` 可用（`ord_sts=U` 未支付）

详细待办见 [docs/HANDOFF.md](docs/HANDOFF.md)（可直接迁移到新对话）。

## 快速开始

```bash
cd 抢票
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env     # 填入一户通用户名和密码
```

首次运行会自动安装 ChromeDriver（需联网一次）。

## 使用流程

```bash
python main.py choose           # 交互式选择 场地/日期/时段，保存到 state/selection.json
python main.py check            # 查询已选场地/日期的时段余量（--date 可指定日期）
python main.py book --dry-run   # 放场前演练：等待放场时间 → 查余量，不下单
python main.py book             # 真抢：提前运行，到点自动执行
python main.py cancel [订单号]  # 取消待付款订单（不传订单号则自动取消当前 Lock 订单）
```

`book` 提前运行时（放场时间未到）会先预热：打开浏览器、加载好预约页，
然后精确等到放场时刻，**自动刷新页面**让目标日期进入可选范围后立刻抢，无需重新启动。

辅助/调试命令：

```bash
python main.py login --fresh    # 用全新浏览器 profile 登录
python main.py discover         # 分步保存页面结构快照
python main.py snap             # 手动操作时按 1 存快照、0 退出（逐步骤取证）
```

## 图形界面（纯鼠标操作）

双击 **`抢票助手.command`**（或终端运行 `python courtbot_gui.py`）会自动打开浏览器页面：

- 下拉选择 日期 / 场地 / 时段，点「保存选择」；
- 操作按钮：`查余量` `演练`（彩排不下单）`抢场` `取消订单` `停止`；
- 抢场识别到支付链接后，`打开支付页` 按钮可用，点击用默认浏览器打开扫码；
- 日志实时显示；会话过期时相关操作会自动重建。

未执行 `choose` 时，`book`/`check` 使用 `config.yaml` 默认配置（三号场、07:00-08:00、日期=放场日+3 天）。

## 抢场时的人工操作

1. 腾讯滑块：提交后如弹出滑块，手动拖动；**当前有“滑块未弹却进入验证码页”的疑点**（见 HANDOFF 问题 1）；
2. 短信验证码：已启用 iMessage 自动读取（`sms.mode: imessage`，需 App 具「完全磁盘访问权限」；可在 config.yaml 改回手动）；
3. 支付：脚本打开中银智慧付二维码页面并通知，用 MPay 扫码（不自动支付）。

## 目录结构

```text
抢票/
├── config.yaml          # 场馆/时间/模式配置（敏感信息在 .env）
├── main.py              # CLI 入口（login/discover/snap/choose/check/book）
├── courtbot/
│   ├── browser.py       # Selenium 封装 + CDP 网络捕获（api-key、venue JWT、下单响应、HAR）
│   ├── login.py         # 一户通登录（OAuth 授权入口）
│   ├── venue.py         # venue.mo.gov.mo：门户入口/申请跳转 URI
│   ├── booking_api.py   # booking.sport.gov.mo：place_list/setting_init/open_time/order/payment
│   ├── captcha.py       # 腾讯滑块（默认手动）
│   ├── sms.py           # 验证码：手动 / 读取 iMessage（预留）
│   ├── payment.py       # 中银智慧付二维码与状态轮询（预留）
│   ├── selection.py     # 场地/日期/时段选择与保存
│   ├── runner.py        # 流程编排、定时、交互式选择、快照
│   └── …
├── state/               # git 忽略：会话、选择、页面快照、接口响应、HAR、日志
└── docs/API_MAP.md      # 接口链路图
```

## 安全说明

- 账号密码只在本地 `.env`；`state/`（含 session.json 的 venue JWT 与 api-key）、`HAR/` 抓包均不入 git；
- 每个会话有效期为 30 分钟，过期会自动重新登录；
- 短信读取只在本机 `~/Library/Messages/chat.db` 只读查询，数据不出电脑。
