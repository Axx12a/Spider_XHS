# Spider_XHS 本地网页控制台

给 [cv-cat/Spider_XHS](https://github.com/cv-cat/Spider_XHS) 补的一套本地网页界面。
原仓库是**底层 API 库**（没有前端、没有服务、没有数据库），这个目录里的文件把它变成了可以直接使用的工具。

> 本控制台只是上层封装，所有小红书接口能力都来自上游 `Spider_XHS`，版权与使用条款以上游仓库为准。

## 功能

| 页面 | 能力 |
|---|---|
| 账号 | 扫码登录、多账号登录态管理（Cookie 隔离）、删除账号 |
| 素材库 | 新建 / 编辑 / 删除素材，图文与视频，支持替换图片 |
| 发布笔记 | 多账号 × 多笔记批量发布、话题、地点、定时、可见范围 |
| 私信 | 多账号会话列表、聊天记录、回复、打开即已读、全部标为已读 |
| 通知 | 钉钉机器人推送（页面关掉也能收到） |

## 新增的文件

| 文件 | 说明 |
|---|---|
| `webui.py` | 网页控制台后端（FastAPI） |
| `webui.html` | 前端页面（单文件，无构建步骤） |
| `xhs_cli.py` | 命令行版本，适合批量 / 定时任务 |
| `启动控制台.bat` | Windows 启动脚本 |
| `app.ico` | 快捷方式图标 |
| `docker-compose.yml` | 可选的容器部署 |

## 对上游的修复

这两个是上游仓库本身的问题，不修就跑不起来：

| 文件 | 问题 |
|---|---|
| `requirements.txt` | `curl_cffi` 锁在 `0.15.0`，但代码用的是 `chrome150` 指纹（见 `xhs_utils/xhs_creator/http.py` 注释），0.15.0 只支持到 `chrome146`，创作者端会直接抛 `ImpersonateError`。已改为 `0.16.3`。 |
| `Dockerfile` | 完全没执行 `npm install`，而签名算法 `require('crypto-js')`，构建出的镜像一用就报 `Cannot find module 'crypto-js'`。已补上，并移除了无意义的 `EXPOSE 5000`。 |

## 安装

需要 **Python 3.10+** 和 **Node.js 20+**（签名算法是 JS）。

```bash
pip install -r requirements.txt
npm ci
```

`npm ci` 只装一个包（crypto-js），但缺了发布功能就跑不了。

## 启动

```bash
python webui.py
```

Windows 下直接双击 `启动控制台.bat`。启动后会打印可访问地址：

```
Spider_XHS 控制台已启动
  本机访问        ：http://127.0.0.1:8848
  手机 / 其它设备 ：http://<Tailscale IP>:8848
  （未监听 0.0.0.0，同一局域网内的其它设备无法访问）
```

## 跨设备访问

控制台**只监听 `127.0.0.1` 和本机的 Tailscale 地址**，不监听 `0.0.0.0`。所以同一局域网和公网都连不上，只有本机和已加入你 tailnet 的设备能访问——这是 socket 层面的限制，不依赖防火墙配置。

Tailscale 地址在启动时自动检测（先问 `tailscale ip -4`，问不到就从 `ipconfig` 里按 CGNAT 网段 `100.64.0.0/10` 找），换机器或 IP 变化都不需要改代码。

> **警告**：这个控制台没有任何登录认证，而它持有小红书账号的 Cookie，可以代表你发笔记、发私信。请只绑定回环地址和 Tailscale 地址，**不要改成 `0.0.0.0`**，否则同一局域网内的任何人都能控制你的账号。

## 命令行版本

```bash
python xhs_cli.py login main
python xhs_cli.py accounts
python xhs_cli.py publish main -t "标题" -d "正文" -i 1.jpg 2.jpg
python xhs_cli.py dm-list main
python xhs_cli.py dm-read main <user_id>
python xhs_cli.py dm-send main <user_id> "在的"
```

## 数据存放

以下内容都保存在本地，且已加入 `.gitignore`，不会提交：

```
accounts/        多账号登录态（含 Cookie，敏感）
settings.json    钉钉 Webhook 与加签密钥（敏感）
library/         素材库（index.json + media/）
uploads/         发布时的临时上传
datas/           采集结果与导出
```

素材的删除和替换会先把文件移入 `library/trash/`，误删可以捞回来。

## 已知限制

- 私信发送**只支持文本**（上游限制，不支持图片和礼物）。
- 群聊的已读回执未实现，目前只处理单聊。
- 已读接口的字段上游没有文档，是实测反推出来的：`chat_id` 是对方的 user_id 裸值，`read_store_id` 取会话的 `max_store_id`。如果上游改了协议，这里需要跟着调。
- 上游仓库没有 LICENSE 文件（虽然 README 挂着 MIT 徽章），使用时请自行评估授权风险。

## 风险提示

所有操作都走逆向接口，存在**限流、风控、封号**风险。建议先用小号低频验证。
