# 控制台详细说明

安装与启动见 [README.md](./README.md)，这里是命令行参考、数据存放位置与实现说明。

---

## 命令行版本

不想开网页时用 `xhs_cli.py`，适合批量与定时任务。登录态与网页版共用同一个 `accounts/` 目录。

```bash
python xhs_cli.py login main                          # 扫码登录并保存登录态
python xhs_cli.py accounts                            # 列出已保存账号

python xhs_cli.py publish main -t "标题" -d "正文" -i 1.jpg 2.jpg
python xhs_cli.py publish main -t "标题" -d "正文" -v clip.mp4 --at "2026-09-30 18:00"

python xhs_cli.py dm-list main                        # 私信会话列表
python xhs_cli.py dm-read main <user_id>              # 某个会话的消息记录
python xhs_cli.py dm-send main <user_id> "在的"        # 回复私信
python xhs_cli.py dm-watch main                       # 实时监听新私信
```

需要代理时给任意命令加 `--proxy http://127.0.0.1:7890`。

---

## 数据存放

全部保存在本地，且已加入 `.gitignore`，不会提交：

| 路径 | 内容 |
|---|---|
| `accounts/` | 多账号登录态（**含 Cookie，敏感**） |
| `settings.json` | 钉钉 Webhook 与加签密钥（**敏感**） |
| `library/` | 素材库（`index.json` + `media/`） |
| `library/trash/` | 被删除或被替换掉的素材文件，**误删可以捞回来** |
| `uploads/` | 发布时的临时上传 |
| `datas/` | 采集结果与导出 |

---

## 实现说明

### 监听地址

`webui.py` 启动时创建两个监听 socket：`127.0.0.1` 和本机 Tailscale 地址，
**不绑定 `0.0.0.0`**。Tailscale 地址的探测顺序是：

1. `tailscale ip -4`（官方 CLI）
2. `ipconfig` 输出中匹配 CGNAT 网段 `100.64.0.0/10`

两者都失败时只监听回环，并在终端明确提示「当前只有本机可访问」，不会静默降级。

### 消息监控

后台线程按固定间隔（默认 10 秒，可在界面上调）遍历所有账号，各抓一次未读数和会话列表。
新消息的判定条件是两个：

1. 未读数增加
2. **或** 最后消息时间前进

只看未读会漏掉「消息到了但你已经在 App 里读过」的情况；而只看时间又会把
**你自己发出去的消息**误判成新私信。所以条件 2 触发时，会回头查一次该会话
最后一条消息的发送者 ID，是自己发的就跳过。

前端通过 SSE（`/api/monitor/events`）接收推送。用长连接而不是前端定时器，
是因为浏览器会把后台标签页的 `setInterval` 限流到分钟级。

### 已读回执

上游的 `mark_messages_read` 要求 `chat_id`、`read_store_id`、`unread_count`、
`type`、`need_rm_offline` 五个字段，但**文档里没有说明 `chat_id` 是什么**，
`get_chats` 接口也不返回这些字段。

实测反推的结论（三种写法服务端都返回 `code: 0 success: true`，但只有第三种真正生效）：

| `chat_id` 写法 | 结果 |
|---|---|
| `对方ID.我的ID` | 返回成功，未读不变 |
| `我的ID.对方ID` | 返回成功，未读不变 |
| **`对方ID`（裸值）** | 未读正确清零 |

`read_store_id` 取会话列表里的 `max_store_id`。

### 钉钉推送

支持加签（HMAC-SHA256）与自定义关键词两种安全模式，见 「通知」页面。
推送在独立线程执行，不阻塞监控轮询；同时做了**每分钟 15 条**的限流
（钉钉官方上限是 20），超出会跳过并在终端打日志。

---

## 常见问题

**发布报 `ImpersonateError: Impersonating chrome150 is not supported`**
`curl_cffi` 版本过低。`pip install -U "curl_cffi>=0.16.2"`。

**发布或签名报 `Cannot find module 'crypto-js'`**
没有装 Node 依赖。在项目根目录执行 `npm ci`。

**手机打不开**
确认手机已连接 Tailscale，且访问的是启动时打印的那个 `100.x.x.x` 地址。

**提示音不响**
浏览器要求先有用户交互才允许播放声音。在页面上随便点一下即可解锁；
Chrome 还需在站点设置里把「声音」设为允许。

**红点一直不消失**
打开会话会自动标记已读。若仍未消失，点右上角「全部标为已读」。
