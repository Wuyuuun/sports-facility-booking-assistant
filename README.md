# 澳门一户通 · 羽毛球抢场工具

针对澳门一户通「场地设施预约」服务编写的自动化抢场工具，目标：**奥林匹克运动场三号羽毛球场**，提前 3 天早上 8:00 放场，抢到后中银智慧付（MPay）扫码支付。

> 仅供个人低频使用。自动化访问政府网站存在使用条款与风控风险，请自行评估；本工具不会自动支付，支付环节始终由你手动扫码完成。

## 目前进度

- ✅ 完成三个 HAR 的分析，接口链路已摸清（见 [docs/API_MAP.md](docs/API_MAP.md)）
- ✅ 登录/会话链路跑通：OAuth 授权入口登录 → 捕获 venue JWT（Authorization）→ 获取 booking `api-key`
- ✅ 场馆/场次查询验证通过：`place/list`、`setting/init`、`open_time` 均已解析，实测可输出三号场余量
- ✅ 交互式选择（`choose`）与余量查询（`check`）可用
- ✅ 抢场主流程已实现：按文本自动点击（日期/场地/时段/提交）→ CDP 捕获 `order/add` → 短信验证码 → BOC 支付二维码
- ⏳ 首次真实抢场时需验证：`order/add` 响应中的订单号字段、`payment/start` 响应中的支付 URL 字段（脚本会把原始响应保存到 `state/responses/`，根据响应补字段即可，无需改动流程）

## 快速开始

```bash
cd 抢票
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env     # 填入你的一户通用户名和密码
```

首次运行会自动安装 ChromeDriver（需要联网一次）。

## 使用流程

```bash
python main.py choose           # 交互式选择 场地(三号场)/日期/时段，保存到 state/selection.json
python main.py check            # 查询已选场地/日期的时段余量（可用 --date 指定日期）
python main.py book --dry-run   # 放场前演练：等待放场时间 → 查询余量，不下单
python main.py book             # 真抢：提前运行，到点自动执行
```

辅助命令：

```bash
python main.py login --fresh    # 用全新浏览器 profile 登录（排查登录问题用）
python main.py discover         # 保存页面结构快照（页面改版排查时用）
python main.py snap             # 手动操作时按 1 存快照、0 退出（逐步骤取证）
```

未执行 `choose` 时，`book`/`check` 会使用 `config.yaml` 里的默认配置（三号场、07:00-08:00、日期=放场日+3 天）。

## 抢场时的人工操作

1. 腾讯滑块：脚本停在提交前并提示，你在弹出的浏览器里手动拖动滑块；
2. 短信验证码：默认手动输入（`sms.mode: manual`）；如需自动读取 Mac 短信，把 `sms.mode` 改为 `imessage` 并按 [courtbot/sms.py](courtbot/sms.py) 中的说明授权完全磁盘访问权限；
3. 支付：脚本打开中银智慧付二维码页面并语音/系统通知提示，你用 MPay 扫码完成支付。

## 目录结构

```text
抢票/
├── config.yaml          # 场馆/时间/模式配置（敏感信息在 .env）
├── main.py              # CLI 入口（login/discover/check/book/choose）
├── courtbot/
│   ├── browser.py       # Selenium 封装 + CDP 网络捕获（api-key、venue JWT、下单响应、HAR）
│   ├── login.py         # 一户通登录（OAuth 授权入口）
│   ├── venue.py         # venue.mo.gov.mo：门户入口/场馆列表/申请跳转 URI
│   ├── booking_api.py   # booking.sport.gov.mo：place_list/setting_init/open_time/order/payment
│   ├── captcha.py       # 腾讯滑块（默认手动）
│   ├── sms.py           # 验证码：手动 / 读取 iMessage（预留）
│   ├── payment.py       # 中银智慧付二维码与状态轮询
│   ├── selection.py     # 场地/日期/时段选择与保存
│   ├── runner.py        # 流程编排、定时与交互式选择
│   └── …
├── state/               # git 忽略：会话、选择、页面快照、接口响应、HAR、日志
└── docs/API_MAP.md      # HAR 分析出的接口链路
```

## 安全说明

- 账号密码只存在本地 `.env`，已加入 `.gitignore`；
- `state/session.json` 含登录 Cookie、venue JWT 与 api-key，已加入 `.gitignore` 并限制权限；
- 三个原始 HAR 已加入 `.gitignore`，请勿上传到任何公开仓库；
- 短信读取只在本机 `~/Library/Messages/chat.db` 做只读查询，数据不出电脑。
