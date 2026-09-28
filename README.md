# Spider_XHS 控制台

**多账号 · 批量发笔记 · 私信收发 · 消息提醒** ，一个本地网页控制台。

> 本仓库是 [cv-cat/Spider_XHS](https://github.com/cv-cat/Spider_XHS) 的 **Fork**。
> 底层的小红书接口能力（签名算法、请求封装、Cookie 维持）全部来自上游；
> 本仓库负责的是上层封装：网页界面、多账号管理、素材库、批量发布、私信、通知。
> 上游原始 README 完整保留在 [README_UPSTREAM.md](./README_UPSTREAM.md)。

---

## 它解决什么问题

上游 `Spider_XHS` 是一个**底层 API 库**——没有界面、没有常驻服务，用之前得先改源码里的常量。
功能齐全，但对不写代码的人不可用。这个 Fork 把它变成了能直接上手的东西。

| | 上游 Spider_XHS | 本仓库 |
|---|---|---|
| 使用方式 | 改代码 + 跑脚本 | 打开网页点几下 |
| 多账号 | 需要自己实现 | 扫码登录，统一管理 |
| 发笔记 | 调 API | 素材库选素材 → 勾账号 → 一键发 |
| 私信 | 调 API | 会话列表 + 聊天窗 + 直接回复 |
| 消息提醒 | 无 | 提示音 + 浏览器通知 + 钉钉 |

---

## 功能

| 页面 | 能力 |
|---|---|
| **账号** | 扫码登录、多账号登录态隔离、删除账号 |
| **素材库** | 图文 / 视频素材的新增、编辑、替换图片、删除（进回收站） |
| **发布笔记** | 多账号 × 多笔记批量发布，支持话题、地点、定时、可见范围 |
| **私信** | 多账号会话列表、聊天记录、回复、打开即已读、一键全部已读 |
| **通知** | 新私信提示音 + 系统通知；钉钉机器人推送（页面关掉也能收到） |

另外附带一个命令行版本 `xhs_cli.py`，适合批量与定时任务，见 [CONSOLE.md](./CONSOLE.md)。

---

## 快速开始

**环境要求**：Python 3.10+、Node.js 20+（签名算法是 JS，必须装 Node）

```bash
git clone https://github.com/Axx12a/Spider_XHS.git
cd Spider_XHS

pip install -r requirements.txt
npm ci                                  # 只装一个包 crypto-js，但缺了发布功能就跑不了

python webui.py
```

然后打开 http://127.0.0.1:8848

Windows 下也可以直接双击 `启动控制台.bat`（会顺带显示可访问地址）。

### Docker

```bash
docker compose up -d
```

国内网络拉不到 Docker Hub 时，基础镜像和 pip 源都可以用构建参数覆盖：

```bash
docker build \
  --build-arg PYTHON_IMAGE=docker.1ms.run/library/python:3.10-slim \
  --build-arg PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple \
  -t spider_xhs .
```

---

## 跨设备访问（Tailscale）

控制台**只监听 `127.0.0.1` 和本机的 Tailscale 地址**，不监听 `0.0.0.0`。
因此同一局域网和公网都连不上，只有本机与已加入你 tailnet 的设备能访问——
这是 socket 层面的限制，不依赖防火墙配置。

启动时会自动探测 Tailscale 地址（先问 `tailscale ip -4`，问不到就从 `ipconfig` 里
按 CGNAT 网段 `100.64.0.0/10` 找），换机器或 IP 变化都不需要改代码。

手机端做了响应式适配，浏览器直接打开即可。

> ### ⚠️ 安全警告
>
> 这个控制台**没有任何登录认证**，而它持有小红书账号的 Cookie，
> 可以代你发笔记、发私信、读取全部私信。
>
> 请**不要**把监听地址改成 `0.0.0.0`。那会让同一局域网内的任何人都能控制你的账号。

---

## 相对上游的修复

上游仓库有两处会导致创作者端（发布）完全不可用，本仓库已修复，并已向上游提交 PR：

| 文件 | 问题 |
|---|---|
| `requirements.txt` | `curl_cffi` 锁在 `0.15.0`，但代码使用 `chrome150` 指纹（见 `xhs_utils/xhs_creator/http.py` 的注释），而 `0.15.0` 最高只支持 `chrome146`，创作者端会直接抛 `ImpersonateError` |
| `Dockerfile` | 未执行 `npm install`，而签名算法 `require('crypto-js')`，构建出的镜像一调用签名就报 `Cannot find module 'crypto-js'` |

PR：<https://github.com/cv-cat/Spider_XHS/pull/189>

---

## 已知限制

- 私信发送**只支持文本**（上游限制，不支持图片和礼物）。
- 群聊的已读回执未实现，目前只处理单聊。
- 已读接口的字段上游没有文档，是实测反推的：`chat_id` 取对方的 user_id 裸值，
  `read_store_id` 取会话的 `max_store_id`。上游若修改协议，这里需要跟着调整。

---

## 免责声明

- 本项目**仅供学习与个人使用**。所有操作都走逆向接口，存在**限流、风控、封号**风险，建议先用小号低频验证。
- 上游 `Spider_XHS` **没有 LICENSE 文件**（尽管其 README 悬挂 MIT 徽章），本仓库同样未声明开源协议。**请勿用于商业用途**，使用前请自行评估授权与合规风险。
- 底层实现与相关版权归 [cv-cat](https://github.com/cv-cat) 所有。如果你觉得这个 Fork 有用，请去给[上游仓库](https://github.com/cv-cat/Spider_XHS)点个 Star。
