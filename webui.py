# encoding: utf-8
"""Spider_XHS 本地网页控制台。

启动：
    .\\.venv\\Scripts\\python.exe webui.py
然后浏览器打开 http://127.0.0.1:8848

只监听本机回环地址，不对外网开放。账号 Cookie 保存在 ./accounts/。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import io
import json
import random
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import uvicorn
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

from xhs_utils.xhs_auth import XHSUnifiedAuth

ROOT = Path(__file__).resolve().parent
ACCOUNT_DIR = ROOT / "accounts"
UPLOAD_DIR = ROOT / "uploads"
LIBRARY_DIR = ROOT / "library"
LIBRARY_MEDIA = LIBRARY_DIR / "media"
LIBRARY_TRASH = LIBRARY_DIR / "trash"
LIBRARY_INDEX = LIBRARY_DIR / "index.json"

app = FastAPI(title="Spider_XHS 控制台")

# 登录是阻塞流程（轮询扫码最多 180s），放到后台线程，前端轮询状态。
_LOGIN: dict[str, Any] = {
    "status": "idle",  # idle / pending / success / error / conflict
    "message": "",
    "qr": None,
    "name": "",
    "nickname": "",
    "conflict": None,
    "warning": "",
    "started_at": 0.0,
    "run_id": 0,
}
_LOGIN_LOCK = threading.Lock()
LOGIN_STALE_SECONDS = 200  # 超过这个时间仍 pending，就允许强制重开

# --------------------------------------------------------------------------- #
# 多账号消息监控：后台轮询每个账号的未读数与会话列表
# --------------------------------------------------------------------------- #

_MONITOR: dict[str, Any] = {
    "running": False,
    "interval": 10,      # 每个账号每轮之间 / 每轮结束后等待的秒数
    "accounts": {},      # name -> {unread_total, unread, chats, updated_at, error}
    "updated_at": 0.0,
}
_MONITOR_LOCK = threading.Lock()
_MONITOR_WAKE = threading.Event()
_MONITOR_THREAD: threading.Thread | None = None
_MONITOR_AUTH: dict[str, Any] = {}   # 监控线程专用的 Auth 缓存，避免每轮都重新 bootstrap

# 扫码成功后先不落盘，等确认没有重名冲突再保存。
# run_id -> {"cookies", "nickname", "user_id"}
_PENDING_LOGIN: dict[int, dict] = {}

# 新消息事件队列：SSE 推给前端，前端据此播放提示音 / 弹系统通知
_EVENTS: list[dict] = []
_EVENT_SEQ = 0
_EVENTS_LOCK = threading.Lock()
MAX_EVENTS = 300


def _push_event(account: str, account_nick: str, chat: dict) -> None:
    global _EVENT_SEQ
    with _EVENTS_LOCK:
        _EVENT_SEQ += 1
        event = {
            "id": _EVENT_SEQ,
            "account": account,
            "account_nickname": account_nick,
            "user_id": chat.get("user_id", ""),
            "nickname": chat.get("nickname", ""),
            "text": chat.get("last", ""),
            "unread": chat.get("unread", 0),
            "time": time.time(),
        }
        _EVENTS.append(event)
        if len(_EVENTS) > MAX_EVENTS:
            del _EVENTS[: len(_EVENTS) - MAX_EVENTS]

    # 钉钉推送：线程内跑，不影响 SSE 和监控轮询
    notify_dingtalk(account, account_nick, chat)


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #


def account_path(name: str) -> Path:
    safe = "".join(ch for ch in name if ch.isalnum() or ch in "-_")
    if not safe:
        raise ValueError("账号名只能包含字母、数字、- 和 _")
    return ACCOUNT_DIR / f"{safe}.json"


def load_account(name: str) -> dict:
    path = account_path(name)
    if not path.is_file():
        raise FileNotFoundError(f"没有找到账号 {name}，请先扫码登录")
    return json.loads(path.read_text(encoding="utf-8"))


def build_auth(name: str) -> XHSUnifiedAuth:
    data = load_account(name)
    try:
        return XHSUnifiedAuth.from_cookie(data["cookies"])
    except Exception as exc:
        raise RuntimeError(
            f"账号 {name} 登录态已失效（{exc}），请重新扫码登录"
        ) from exc


def read_account_file(name: str) -> dict | None:
    path = account_path(name)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def find_account_by_user_id(user_id: str, exclude: str = "") -> dict | None:
    """按小红书 user_id 查已保存的账号。

    同一个号可能被存成多个名字（重复登录），所以去重必须看 user_id，
    不能只看账号名。
    """
    if not user_id or not ACCOUNT_DIR.is_dir():
        return None
    for path in ACCOUNT_DIR.glob("*.json"):
        if path.stem == exclude:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("user_id") == user_id:
            return data
    return None


def save_account_file(
    name: str, cookies: str, nickname: str, user_id: str, **extra: Any
) -> Path:
    ACCOUNT_DIR.mkdir(exist_ok=True)
    path = account_path(name)
    payload = {
        "name": name,
        "cookies": cookies,
        "nickname": nickname,
        "user_id": user_id,
        "saved_at": int(time.time()),
        **extra,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def suggest_account_name(name: str) -> str:
    """账号名已被别的号占用时，给一个可用的建议名：xxx-2 / xxx-3 …"""
    for i in range(2, 100):
        candidate = f"{name}-{i}"
        if not account_path(candidate).is_file():
            return candidate
    return f"{name}-{int(time.time())}"


def qr_data_url(url: str) -> str:
    import qrcode

    img = qrcode.make(url)
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


def err(message: str, code: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=code)


# --------------------------------------------------------------------------- #
# 素材库存储
# --------------------------------------------------------------------------- #


def load_library() -> list[dict]:
    if not LIBRARY_INDEX.is_file():
        return []
    try:
        data = json.loads(LIBRARY_INDEX.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_library(items: list[dict]) -> None:
    LIBRARY_DIR.mkdir(exist_ok=True)
    LIBRARY_INDEX.write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def move_to_trash(path: str | Path) -> None:
    """删除的素材文件先挪进 library/trash/，误删还能捞回来。"""
    source = Path(path)
    if not source.is_file():
        return
    try:
        LIBRARY_TRASH.mkdir(parents=True, exist_ok=True)
        target = LIBRARY_TRASH / f"{int(time.time() * 1000)}_{source.name}"
        source.replace(target)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 通知设置（钉钉机器人）
# --------------------------------------------------------------------------- #

SETTINGS_PATH = ROOT / "settings.json"

DEFAULT_SETTINGS: dict[str, Any] = {
    "dingtalk": {
        "enabled": False,
        "webhook": "",
        "secret": "",
        "keyword": "",
        "at_mobiles": "",
        "at_all": False,
        "accounts": "",
    },
    # AI 生成素材用。文案和绘图分开配置，可以混用不同服务商。
    # 都走 OpenAI 兼容接口，所以换服务商只需要改 base_url / model。
    "ai": {
        # openai = 任意 OpenAI 兼容服务（DeepSeek / 智谱 / 硅基流动 …）
        # pollinations = Pollinations 免费额度，无需密钥但随时可能被限
        "text_provider": "openai",
        # 智谱 GLM-4-Flash 免费，是性价比最高的默认选择
        "text_base_url": "https://open.bigmodel.cn/api/paas/v4",
        "text_api_key": "",
        "text_model": "glm-4-flash",
        # pollinations 绘图免费且不需要密钥，所以默认选它
        "image_provider": "pollinations",
        # 如果已经有智谱 key，绘图也可以切到 openai 兼容 + cogview-3-flash（同样免费）
        "image_base_url": "https://open.bigmodel.cn/api/paas/v4",
        "image_api_key": "",
        "image_model": "cogview-3-flash",
        "image_size": "1024x1536",
    },
}


def load_settings() -> dict:
    if not SETTINGS_PATH.is_file():
        return json.loads(json.dumps(DEFAULT_SETTINGS))
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return json.loads(json.dumps(DEFAULT_SETTINGS))
    for key, value in DEFAULT_SETTINGS.items():
        if isinstance(value, dict):
            data.setdefault(key, {})
            for sub, sub_value in value.items():
                data[key].setdefault(sub, sub_value)
    return data


def save_settings(data: dict) -> None:
    SETTINGS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def send_dingtalk(title: str, body: str, cfg: dict) -> tuple[bool, str]:
    """给钉钉自定义机器人发一条 markdown 消息。

    支持加签（secret）与关键词校验（keyword 会拼进标题）。
    """
    import requests

    webhook = (cfg.get("webhook") or "").strip()
    if not webhook:
        return False, "没有填 Webhook 地址"

    keyword = (cfg.get("keyword") or "").strip()
    final_title = f"{keyword} {title}".strip()

    url = webhook
    secret = (cfg.get("secret") or "").strip()
    if secret:
        stamp = str(round(time.time() * 1000))
        string_to_sign = f"{stamp}\n{secret}"
        digest = hmac.new(
            secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256
        ).digest()
        sign = quote_plus(base64.b64encode(digest))
        joiner = "&" if "?" in url else "?"
        url = f"{url}{joiner}timestamp={stamp}&sign={sign}"

    mobiles = [m.strip() for m in (cfg.get("at_mobiles") or "").replace("，", ",").split(",") if m.strip()]
    payload = {
        "msgtype": "markdown",
        "markdown": {"title": final_title, "text": body},
        "at": {"atMobiles": mobiles, "isAtAll": bool(cfg.get("at_all"))},
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        result = resp.json()
    except Exception as exc:
        return False, f"请求失败：{exc}"

    if result.get("errcode") == 0:
        return True, "发送成功"
    return False, f"钉钉返回：{result.get('errcode')} {result.get('errmsg')}"


# 钉钉自定义机器人限流：每个机器人每分钟最多 20 条，这里留点余量
_DT_SENT: list[float] = []
_DT_LOCK = threading.Lock()
DT_MAX_PER_MINUTE = 15


def _dt_rate_ok() -> bool:
    now = time.time()
    with _DT_LOCK:
        _DT_SENT[:] = [t for t in _DT_SENT if now - t < 60]
        if len(_DT_SENT) >= DT_MAX_PER_MINUTE:
            return False
        _DT_SENT.append(now)
        return True


def notify_dingtalk(account: str, account_nick: str, chat: dict) -> None:
    """有新私信时推送到钉钉。放到独立线程，避免拖慢监控轮询。"""
    try:
        cfg = load_settings().get("dingtalk") or {}
    except Exception:
        return
    if not cfg.get("enabled") or not (cfg.get("webhook") or "").strip():
        return

    allow = [a.strip() for a in (cfg.get("accounts") or "").replace("，", ",").split(",") if a.strip()]
    if allow and account not in allow:
        return

    if not _dt_rate_ok():
        print("[dingtalk] 一分钟内推送已达上限，本条跳过")
        return

    title = "小红书新私信"
    body = (
        f"### 小红书收到新私信\n\n"
        f"- **账号**：{account_nick}（{account}）\n"
        f"- **来自**：{chat.get('nickname', '')}\n"
        f"- **内容**：{chat.get('last', '') or '(非文本消息)'}\n"
        f"- **未读**：{chat.get('unread', 0)} 条\n"
    )
    mobiles = [m.strip() for m in (cfg.get("at_mobiles") or "").replace("，", ",").split(",") if m.strip()]
    if mobiles:
        body += "\n" + " ".join(f"@{m}" for m in mobiles)

    def worker() -> None:
        ok, msg = send_dingtalk(title, body, cfg)
        if not ok:
            print(f"[dingtalk] 推送失败：{msg}")

    threading.Thread(target=worker, daemon=True).start()


# --------------------------------------------------------------------------- #
# AI 生成素材（OpenAI 兼容接口）
#
# 文案和绘图分成两套配置，因为常见组合是「DeepSeek 写文案 + 智谱/硅基流动画图」。
# 两边分别打 OpenAI 的 /chat/completions 与 /images/generations，
# 换服务商只要改 base_url 和 model，不用动代码。
# --------------------------------------------------------------------------- #


def ai_config() -> dict:
    cfg = dict(DEFAULT_SETTINGS["ai"])
    cfg.update(load_settings().get("ai") or {})
    return cfg


def _openai_post(base_url: str, path: str, key: str, payload: dict, timeout: float = 180):
    import requests

    url = base_url.rstrip("/") + path
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=payload,
        timeout=timeout,
    )
    # 这两个错误最常见的根因是「Key 和 Base URL 不是同一家的」，
    # 原始返回是服务商自己的黑话，直接抛出去用户看不懂，这里补上人话。
    if resp.status_code == 401:
        raise RuntimeError(
            f"认证失败（401）。当前请求发往 {base_url}，"
            "请确认这个 API Key 是在该平台申请的 —— "
            "把智谱的 Key 填到 DeepSeek 的地址上就会报这个。"
            f"　原始返回：{resp.text[:200]}"
        )
    if resp.status_code == 404:
        raise RuntimeError(
            f"接口不存在（404）：{url}　请检查 Base URL。"
            "智谱是 https://open.bigmodel.cn/api/paas/v4 ，"
            "DeepSeek 是 https://api.deepseek.com/v1 。"
            f"　原始返回：{resp.text[:200]}"
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def _parse_json_block(text: str) -> dict:
    """模型经常把 JSON 包在 ``` 里或前后带解释，这里做容错解析。"""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        data = json.loads(raw[start : end + 1])
        if isinstance(data, dict):
            return data
    raise RuntimeError("模型没有返回可解析的 JSON")


def _build_note_messages(topic: str, style: str, count: int, extra: str = "") -> list[dict]:
    system = (
        "你是资深小红书运营，擅长写高互动率的图文笔记。"
        "只输出 JSON，不要解释、不要代码块标记。"
    )
    user = (
        f"主题：{topic}\n"
        f"风格：{style or '种草分享'}\n"
        f"需要配图数量：{count}\n\n"
        "输出这个 JSON：\n"
        "{\n"
        '  "title": "标题，20 字以内，有钩子",\n'
        '  "desc": "正文，250-450 字，分 3-5 段，口语化，可带 emoji，结尾引导评论",\n'
        '  "topics": ["话题1", "话题2"],\n'
        '  "image_prompts": ["英文绘图提示词"]\n'
        "}\n"
        "topics 给 5-8 个，不带 # 号。\n"
        f"image_prompts 数量必须等于 {count}，每个描述具体画面、构图、色调，"
        "是给文生图模型用的英文提示词，画面里不要出现文字或水印。"
    )
    if extra.strip():
        user += f"\n\n补充要求（必须遵守，优先级高于上面的默认风格）：\n{extra.strip()}"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _pollinations_text(messages: list[dict], cfg: dict) -> str:
    import requests

    body = {
        "model": (cfg.get("text_model") or "").strip() or "openai",
        "messages": messages,
        "private": True,
    }
    resp = requests.post("https://text.pollinations.ai/openai", json=body, timeout=120)
    resp.raise_for_status()
    try:
        content = resp.json()["choices"][0]["message"]["content"]
    except Exception as exc:
        raise RuntimeError(f"Pollinations 返回异常：{resp.text[:200]}") from exc
    # Pollinations 会把「额度用尽」也当成正常正文返回，必须拦下来，
    # 否则这段英文提示会被当成笔记内容存进素材库。
    low = (content or "").lower()
    if "budget" in low or "pollinations.ai/edit-key" in low:
        raise RuntimeError(
            "Pollinations 的免费文本额度已用尽。建议改用其它 OpenAI 兼容服务"
            "（例如 DeepSeek），或去 pollinations.ai 注册拿 token。"
        )
    return content or ""


def ai_write_note(topic: str, style: str, count: int, cfg: dict, extra: str = "") -> dict:
    messages = _build_note_messages(topic, style, count, extra)
    provider = (cfg.get("text_provider") or "openai").lower()

    if provider == "pollinations":
        content = _pollinations_text(messages, cfg)
    else:
        if not (cfg.get("text_api_key") or "").strip():
            raise RuntimeError("还没有配置文案模型的 API Key")
        data = _openai_post(
            cfg["text_base_url"],
            "/chat/completions",
            cfg["text_api_key"],
            {
                "model": cfg["text_model"],
                "messages": messages,
                "temperature": 0.9,
            },
        )
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"文案接口返回结构异常：{str(data)[:200]}") from exc
    return _parse_json_block(content)


def _image_size(cfg: dict) -> tuple[int, int]:
    raw = (cfg.get("image_size") or "1024x1536").lower().replace(" ", "").replace("*", "x")
    try:
        width, height = (int(v) for v in raw.split("x", 1))
        if width > 0 and height > 0:
            return width, height
    except Exception:
        pass
    return 1024, 1536


def _pollinations_image(prompt: str, cfg: dict) -> bytes:
    """Pollinations 免密钥文生图：直接把提示词拼进 URL。"""
    import requests
    from urllib.parse import quote

    width, height = _image_size(cfg)
    url = (
        "https://image.pollinations.ai/prompt/"
        + quote(prompt, safe="")
        + f"?width={width}&height={height}&nologo=true&seed={random.randint(1, 10 ** 9)}"
    )
    # 这里刻意不传 model：image_model 是给 OpenAI 兼容通道用的（默认是智谱的
    # cogview-3-flash），传给 Pollinations 会报模型不存在。Pollinations 目前
    # 只有一个模型，不传就是用它自己的默认值。
    resp = requests.get(url, timeout=180)
    resp.raise_for_status()
    ctype = (resp.headers.get("content-type") or "").lower()
    if not ctype.startswith("image/"):
        raise RuntimeError(f"Pollinations 没有返回图片：{resp.text[:200]}")
    return resp.content


def ai_make_image(prompt: str, cfg: dict) -> bytes:
    provider = (cfg.get("image_provider") or "pollinations").lower()
    if provider == "pollinations":
        return _pollinations_image(prompt, cfg)

    import requests

    if not (cfg.get("image_api_key") or "").strip():
        raise RuntimeError("还没有配置绘图模型的 API Key")
    data = _openai_post(
        cfg["image_base_url"],
        "/images/generations",
        cfg["image_api_key"],
        {
            "model": cfg["image_model"],
            "prompt": prompt,
            "n": 1,
            "size": cfg.get("image_size") or "1024x1024",
        },
    )
    items = data.get("data") or []
    item = items[0] if items else {}
    if item.get("b64_json"):
        return base64.b64decode(item["b64_json"])
    url = item.get("url")
    if not url:
        raise RuntimeError(f"绘图接口没有返回图片：{str(data)[:200]}")
    resp = requests.get(url, timeout=180)
    resp.raise_for_status()
    return resp.content


@app.post("/api/ai/generate")
def api_ai_generate(payload: dict) -> Any:
    topic = (payload.get("topic") or "").strip()
    if not topic:
        return err("请先填写主题")
    style = (payload.get("style") or "").strip()
    extra = (payload.get("extra") or "").strip()
    try:
        count = max(0, min(6, int(payload.get("image_count", 2))))
    except (TypeError, ValueError):
        count = 2

    cfg = ai_config()
    try:
        note = ai_write_note(topic, style, count, cfg, extra)
    except Exception as exc:
        return err(f"文案生成失败：{exc}")

    title = str(note.get("title") or topic).strip()
    desc = str(note.get("desc") or "").strip()
    topics = [str(t).lstrip("#").strip() for t in (note.get("topics") or []) if str(t).strip()]
    prompts = [str(p).strip() for p in (note.get("image_prompts") or []) if str(p).strip()][:count]

    # 配图并发生成：逐张串行会等到天荒地老
    images: list[str] = []
    image_errors: list[str] = []
    # Pollinations 不需要密钥，所以「有没有 key」不能作为能不能画图的判断
    image_ready = (
        (cfg.get("image_provider") or "pollinations").lower() == "pollinations"
        or bool((cfg.get("image_api_key") or "").strip())
    )
    if count and prompts and image_ready:
        from concurrent.futures import ThreadPoolExecutor

        LIBRARY_MEDIA.mkdir(parents=True, exist_ok=True)
        stamp = str(int(time.time() * 1000))
        with ThreadPoolExecutor(max_workers=min(4, len(prompts))) as pool:
            futures = {pool.submit(ai_make_image, p, cfg): i for i, p in enumerate(prompts)}
            blobs: dict[int, bytes] = {}
            for future, index in futures.items():
                try:
                    blobs[index] = future.result()
                except Exception as exc:
                    image_errors.append(str(exc))
        for index in sorted(blobs):
            target = LIBRARY_MEDIA / f"{stamp}_ai{index}.png"
            target.write_bytes(blobs[index])
            images.append(str(target))

    material = {
        "id": str(int(time.time() * 1000)),
        "title": title,
        "desc": desc,
        "topics": topics,
        "location": "",
        "created_at": int(time.time()),
        "media_type": "image",
        "images": images,
        "source": "ai",
        "ai_topic": topic,
        "ai_style": style,
    }
    if images:
        items = load_library()
        items.insert(0, material)
        save_library(items)

    return {
        "ok": True,
        "material": material,
        "saved": bool(images),
        "image_errors": image_errors,
    }


@app.get("/api/settings/ai")
def api_ai_get() -> Any:
    return {"ok": True, "ai": ai_config()}


@app.post("/api/settings/ai")
def api_ai_save(payload: dict) -> Any:
    data = load_settings()
    cfg = data.setdefault("ai", {})
    for key in DEFAULT_SETTINGS["ai"]:
        if key in payload:
            cfg[key] = str(payload[key]).strip()
    save_settings(data)
    return {"ok": True, "ai": ai_config()}


def parse_schedule(value: str) -> int | None:
    """把前端传来的时间字符串转成发布接口要的 13 位毫秒时间戳。"""
    text = (value or "").strip()
    if not text:
        return None
    from datetime import datetime

    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(text, fmt).timestamp() * 1000)
        except ValueError:
            continue
    raise ValueError("无法识别定时时间，请填 2026-09-30 18:00 这样的格式")


def split_topics(value: str) -> list[str]:
    if not value or not value.strip():
        return []
    return [t.strip() for t in value.replace("，", ",").split(",") if t.strip()]


def material_to_note(material: dict, privacy: int, post_time: int | None) -> dict:
    """把素材库条目转成 post_note 需要的参数。"""
    note: dict[str, Any] = {
        "title": material.get("title", ""),
        "desc": material.get("desc", ""),
        "type": privacy,
        "media_type": material.get("media_type", "image"),
    }
    if note["media_type"] == "video":
        note["video"] = material.get("video", "")
    else:
        note["images"] = list(material.get("images") or [])
    topics = material.get("topics") or []
    if topics:
        note["topics"] = topics
    if material.get("location"):
        note["location"] = material["location"]
    if post_time:
        note["postTime"] = post_time
    return note


# --------------------------------------------------------------------------- #
# 页面
# --------------------------------------------------------------------------- #


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    html = (ROOT / "webui.html").read_text(encoding="utf-8")
    # 必须禁缓存：这个页面是本地开发用的，改完刷新就该看到新版。
    # 之前没设这个头，浏览器会启发式缓存，导致改完还是显示旧界面。
    return HTMLResponse(
        html,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


# --------------------------------------------------------------------------- #
# 账号 / 登录
# --------------------------------------------------------------------------- #


@app.get("/api/accounts")
def api_accounts() -> Any:
    start_monitor()  # 保证账号状态列有数据可看
    if not ACCOUNT_DIR.is_dir():
        return {"ok": True, "accounts": []}
    rows = []
    for path in sorted(ACCOUNT_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        name = data.get("name", path.stem)
        with _MONITOR_LOCK:
            info = _MONITOR["accounts"].get(name)
        if not info:
            status, status_text = "unknown", "尚未检查"
        elif not info.get("error_count"):
            status, status_text = "ok", "正常"
        else:
            status = info.get("error_kind") or "unknown"
            status_text = info.get("error") or "异常"
        rows.append(
            {
                "name": name,
                "nickname": data.get("nickname") or "",
                "user_id": data.get("user_id") or "",
                "saved_at": data.get("saved_at") or 0,
                "status": status,
                "status_text": status_text,
            }
        )
    return {"ok": True, "accounts": rows}


@app.delete("/api/accounts/{name}")
def api_account_delete(name: str) -> Any:
    path = account_path(name)
    if path.is_file():
        path.unlink()
    # 同步清掉监控缓存，否则账号删了列表里还在
    _MONITOR_AUTH.pop(name, None)
    with _MONITOR_LOCK:
        _MONITOR["accounts"].pop(name, None)
        _MONITOR["updated_at"] = time.time()
    return {"ok": True}


@app.post("/api/accounts/{name}/rename")
def api_account_rename(name: str, payload: dict) -> Any:
    """改本地账号名（就是 accounts/ 下的文件名），不影响小红书账号本身。

    Cookie、user_id 全部原样保留，只是换个你自己看得懂的名字。
    """
    new_name = (payload.get("new_name") or "").strip()
    if not new_name:
        return err("请填写新的账号名")
    try:
        new_path = account_path(new_name)
        old_path = account_path(name)
    except ValueError as exc:
        return err(str(exc))

    if not old_path.is_file():
        return err(f"账号「{name}」不存在", 404)
    if new_name == name:
        return {"ok": True, "name": new_name, "unchanged": True}
    if new_path.exists():
        return err(f"账号名「{new_name}」已经被占用了，换一个吧")

    data = read_account_file(name) or {}
    data["name"] = new_name
    data["renamed_at"] = int(time.time())
    new_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    old_path.unlink()

    # 监控缓存用账号名做 key，同步搬过去，避免要等下一轮抓取
    with _MONITOR_LOCK:
        if name in _MONITOR["accounts"]:
            _MONITOR["accounts"][new_name] = _MONITOR["accounts"].pop(name)
    if name in _MONITOR_AUTH:
        _MONITOR_AUTH[new_name] = _MONITOR_AUTH.pop(name)

    return {"ok": True, "name": new_name, "old_name": name}


def _set_login(run_id: int, **fields: Any) -> None:
    """只允许「当前这一轮」登录流程写状态，防止旧线程回来覆盖新流程。"""
    with _LOGIN_LOCK:
        if _LOGIN.get("run_id") != run_id:
            return
        _LOGIN.update(fields)


def _login_worker(name: str, run_id: int) -> None:
    try:
        from apis.xhs_pc_login_apis import XHSLoginApi

        # 登录流程内部把二维码打到终端；这里改成只捕获 URL，交给前端渲染。
        def capture(url: str) -> None:
            try:
                _set_login(run_id, qr=qr_data_url(url), message="请用小红书 App 扫码并在手机上确认")
            except Exception:
                pass

        XHSLoginApi.show_qrcode_terminal = staticmethod(capture)
        _set_login(run_id, message="正在获取二维码...", qr=None)

        auth = XHSUnifiedAuth.from_qrcode_login(show_in_terminal=True)
        try:
            nickname = ""
            user_id = getattr(auth.pc_auth, "user_id", "") or ""
            try:
                from apis.xhs_pc_apis import XHS_Apis

                ok, _, data = XHS_Apis(auth.pc).bootstrap().get_user_me()
                if ok and isinstance(data, dict):
                    basic = data.get("data") or data
                    nickname = basic.get("nickname", "") or nickname
                    user_id = basic.get("user_id", "") or user_id
            except Exception:
                pass

            # 扫码结果先暂存，确认没有重名冲突再落盘。
            # 直接按账号名写文件的话，「同名但不同号」会静默顶掉上一个账号。
            _PENDING_LOGIN[run_id] = {
                "cookies": auth.pc_auth.cookies,
                "nickname": nickname,
                "user_id": user_id,
            }

            existing = read_account_file(name)
            if (
                existing
                and user_id
                and existing.get("user_id")
                and existing["user_id"] != user_id
            ):
                _set_login(
                    run_id,
                    status="conflict",
                    message=(
                        f"账号名「{name}」已经属于「{existing.get('nickname') or '另一个账号'}」，"
                        f"而这次扫码登录的是「{nickname or '未知'}」。"
                    ),
                    nickname=nickname,
                    qr=None,
                    conflict={
                        "name": name,
                        "existing_nickname": existing.get("nickname") or "",
                        "existing_user_id": existing.get("user_id") or "",
                        "new_nickname": nickname,
                        "new_user_id": user_id,
                        "suggest": suggest_account_name(name),
                    },
                )
                return

            duplicated = find_account_by_user_id(user_id, exclude=name)
            save_account_file(name, auth.pc_auth.cookies, nickname, user_id)
            _PENDING_LOGIN.pop(run_id, None)
            _reset_monitor_account(name)
            _set_login(
                run_id,
                status="success",
                message=f"登录成功：{nickname or name}",
                nickname=nickname,
                qr=None,
                warning=(
                    f"同一个账号之前已保存为「{duplicated.get('name')}」，现在多存了一份，"
                    "不需要的话可以在账号列表里删掉一个。"
                    if duplicated
                    else ""
                ),
                conflict=None,
            )
        finally:
            auth.close()
    except Exception as exc:
        _set_login(run_id, status="error", message=f"登录失败：{exc}", qr=None)


@app.post("/api/login/start")
def api_login_start(name: str = Form(...), force: bool = Form(False)) -> Any:
    with _LOGIN_LOCK:
        if _LOGIN["status"] == "pending" and not force:
            waited = time.time() - float(_LOGIN.get("started_at") or 0)
            if waited < LOGIN_STALE_SECONDS:
                return err(
                    f"上一轮登录还在等扫码（已 {int(waited)} 秒）。"
                    "请扫码，或点「重置登录」后重新发起。"
                )
        _LOGIN["run_id"] = int(_LOGIN.get("run_id") or 0) + 1
        run_id = _LOGIN["run_id"]
        _LOGIN.update(
            {
                "status": "pending",
                "message": "正在初始化...",
                "qr": None,
                "name": name,
                "nickname": "",
                "conflict": None,
                "warning": "",
                "started_at": time.time(),
            }
        )
    threading.Thread(target=_login_worker, args=(name, run_id), daemon=True).start()
    return {"ok": True, "run_id": run_id}


@app.post("/api/login/cancel")
def api_login_cancel() -> Any:
    """重置登录状态；会作废正在跑的那一轮，让用户可以立刻重新扫。"""
    with _LOGIN_LOCK:
        _LOGIN.update(
            {
                "status": "idle",
                "message": "已重置，可以重新发起登录",
                "qr": None,
                "name": "",
                "nickname": "",
                "conflict": None,
                "warning": "",
                "started_at": 0.0,
                "run_id": int(_LOGIN.get("run_id") or 0) + 1,
            }
        )
    return {"ok": True}


@app.post("/api/login/save")
def api_login_save(payload: dict) -> Any:
    """扫码成功后由用户确认账号名，这里才真正落盘。

    用于处理「同名但不同号」的冲突：可以换个名字保存，也可以明确选择覆盖。
    结果暂存在内存里，所以不需要重新扫码。
    """
    try:
        run_id = int(payload.get("run_id") or 0)
    except (TypeError, ValueError):
        return err("run_id 无效")

    name = (payload.get("name") or "").strip()
    overwrite = bool(payload.get("overwrite"))
    if not name:
        return err("请填写账号名")
    try:
        account_path(name)  # 触发账号名的合法性校验
    except ValueError as exc:
        return err(str(exc))

    pending = _PENDING_LOGIN.get(run_id)
    if not pending:
        return err("这次登录的结果已经失效，请重新扫码")

    user_id = pending.get("user_id") or ""
    existing = read_account_file(name)
    if (
        existing
        and not overwrite
        and user_id
        and existing.get("user_id")
        and existing["user_id"] != user_id
    ):
        return err(
            f"账号名「{name}」已经被「{existing.get('nickname') or '另一个账号'}」占用。"
            "请换个名字，或勾选覆盖。"
        )

    save_account_file(name, pending["cookies"], pending.get("nickname", ""), user_id)
    _PENDING_LOGIN.pop(run_id, None)
    _reset_monitor_account(name)
    _set_login(
        run_id,
        status="success",
        message=f"已保存为「{name}」",
        nickname=pending.get("nickname", ""),
        conflict=None,
        warning="",
    )
    return {"ok": True, "name": name}


@app.get("/api/login/state")
def api_login_state() -> Any:
    with _LOGIN_LOCK:
        return {"ok": True, **_LOGIN}


# --------------------------------------------------------------------------- #
# 素材库
# --------------------------------------------------------------------------- #


@app.get("/api/library")
def api_library_list() -> Any:
    return {"ok": True, "materials": load_library()}


@app.post("/api/library")
def api_library_create(
    title: str = Form(...),
    desc: str = Form(...),
    topics: str = Form(""),
    location: str = Form(""),
    images: list[UploadFile] = File(default=[]),
    video: UploadFile | None = File(default=None),
) -> Any:
    try:
        LIBRARY_MEDIA.mkdir(parents=True, exist_ok=True)
        stamp = str(int(time.time() * 1000))
        material: dict[str, Any] = {
            "id": stamp,
            "title": title,
            "desc": desc,
            "topics": split_topics(topics),
            "location": location.strip(),
            "created_at": int(time.time()),
        }

        if video is not None and video.filename:
            suffix = Path(video.filename).suffix or ".mp4"
            target = LIBRARY_MEDIA / f"{stamp}_video{suffix}"
            target.write_bytes(video.file.read())
            material.update(media_type="video", video=str(target), images=[])
        else:
            saved = []
            for item in images:
                if not item.filename:
                    continue
                suffix = Path(item.filename).suffix or ".jpg"
                target = LIBRARY_MEDIA / f"{stamp}_{len(saved)}{suffix}"
                target.write_bytes(item.file.read())
                saved.append(str(target))
            if not saved:
                return err("素材至少要有一张图片，或者一段视频")
            material.update(media_type="image", images=saved)

        items = load_library()
        items.insert(0, material)
        save_library(items)
        return {"ok": True, "material": material}
    except Exception as exc:
        traceback.print_exc()
        return err(str(exc), 500)


@app.put("/api/library/{material_id}")
def api_library_update(
    material_id: str,
    title: str = Form(...),
    desc: str = Form(...),
    topics: str = Form(""),
    location: str = Form(""),
    replace_media: bool = Form(False),
    images: list[UploadFile] = File(default=[]),
    video: UploadFile | None = File(default=None),
) -> Any:
    """编辑素材：文字总是覆盖；只有 replace_media=true 才替换图片/视频。"""
    try:
        items = load_library()
        target = next((m for m in items if m.get("id") == material_id), None)
        if target is None:
            return err("素材不存在", 404)

        target["title"] = title
        target["desc"] = desc
        target["topics"] = split_topics(topics)
        target["location"] = location.strip()
        target["updated_at"] = int(time.time())

        if replace_media:
            old = list(target.get("images") or [])
            if target.get("video"):
                old.append(target["video"])

            if video is not None and video.filename:
                LIBRARY_MEDIA.mkdir(parents=True, exist_ok=True)
                suffix = Path(video.filename).suffix or ".mp4"
                new_path = LIBRARY_MEDIA / f"{material_id}_v{int(time.time())}{suffix}"
                new_path.write_bytes(video.file.read())
                target.update(media_type="video", video=str(new_path), images=[])
            else:
                saved = []
                LIBRARY_MEDIA.mkdir(parents=True, exist_ok=True)
                for item in images:
                    if not item.filename:
                        continue
                    suffix = Path(item.filename).suffix or ".jpg"
                    new_path = LIBRARY_MEDIA / f"{material_id}_r{int(time.time())}_{len(saved)}{suffix}"
                    new_path.write_bytes(item.file.read())
                    saved.append(str(new_path))
                if saved:
                    target.update(media_type="image", images=saved, video="")
                elif not target.get("images") and not target.get("video"):
                    return err("替换素材时至少要提供一张图片或一段视频")

            for path in old:  # 换了新素材，旧文件进回收站而不是直接删
                move_to_trash(path)

        save_library(items)
        return {"ok": True, "material": target}
    except Exception as exc:
        traceback.print_exc()
        return err(str(exc), 500)


@app.delete("/api/library/{material_id}")
def api_library_delete(material_id: str) -> Any:
    items = load_library()
    kept = []
    for item in items:
        if item.get("id") == material_id:
            for path in (item.get("images") or []) + ([item["video"]] if item.get("video") else []):
                move_to_trash(path)
            continue
        kept.append(item)
    save_library(kept)
    return {"ok": True}


@app.post("/api/library/delete")
def api_library_delete_many(payload: dict) -> Any:
    """批量删除素材。文件同样先进回收站，误删可以捞回来。"""
    ids = {str(i) for i in (payload.get("ids") or [])}
    if not ids:
        return err("没有选中任何素材")

    items = load_library()
    kept: list[dict] = []
    removed = 0
    for item in items:
        if str(item.get("id")) in ids:
            for path in (item.get("images") or []) + (
                [item["video"]] if item.get("video") else []
            ):
                move_to_trash(path)
            removed += 1
            continue
        kept.append(item)
    save_library(kept)
    return {"ok": True, "removed": removed}


@app.get("/api/library/{material_id}/file/{index}")
def api_library_file(material_id: str, index: int) -> Any:
    for item in load_library():
        if item.get("id") != material_id:
            continue
        files = list(item.get("images") or [])
        if item.get("video"):
            files.append(item["video"])
        if 0 <= index < len(files):
            path = Path(files[index])
            if path.is_file():
                return FileResponse(str(path))
    return err("文件不存在", 404)


# --------------------------------------------------------------------------- #
# 发布（支持多账号 × 多笔记）
# --------------------------------------------------------------------------- #


# 发布任务：长任务放后台跑，前端轮询进度。
# 如果按同步请求处理，间隔几分钟时一个请求要挂几十分钟，浏览器早断了。
PUBLISH_JOBS: dict[str, dict] = {}
_PUBLISH_LOCK = threading.Lock()
PUBLISH_JOB_KEEP = 10          # 内存里只保留最近 N 个任务
# 默认按「跑矩阵」的场景给：一个账号一天本来就只该发一两条，
# 几十条任务摊到十几分钟一条，整批会跑几个小时，属于正常节奏。
DEFAULT_DELAY_MIN = 600        # 默认 10 分钟
DEFAULT_DELAY_MAX = 1200       # 默认 20 分钟


def _job_patch(job_id: str, **fields: Any) -> None:
    with _PUBLISH_LOCK:
        job = PUBLISH_JOBS.get(job_id)
        if job is not None:
            job.update(fields)


def _job_cancelled(job_id: str) -> bool:
    with _PUBLISH_LOCK:
        return bool(PUBLISH_JOBS.get(job_id, {}).get("cancel"))


def _publish_worker(
    job_id: str,
    account_names: list[str],
    picked: list[dict],
    privacy: int,
    post_time: int | None,
    delay_min: float,
    delay_max: float,
) -> None:
    from apis.xhs_creator_apis import XHS_Creator_Apis

    total = len(account_names) * len(picked)
    results: list[dict] = []

    def record(account: str, title: str, ok: bool, message: str) -> None:
        results.append(
            {"account": account, "title": title, "ok": bool(ok), "message": str(message)}
        )
        _job_patch(
            job_id,
            results=list(results),
            done=len(results),
            succeeded=sum(1 for r in results if r["ok"]),
        )

    def wait_between() -> bool:
        """到下一条之前等待；被取消则返回 False。"""
        if len(results) >= total:
            return True
        wait = random.uniform(delay_min, delay_max)  # 随机抖动，固定间隔本身也是特征
        if wait <= 0:
            return True
        end = time.time() + wait
        _job_patch(job_id, next_at=end)
        while time.time() < end:
            if _job_cancelled(job_id):
                return False
            time.sleep(0.5)
        _job_patch(job_id, next_at=0)
        return True

    # 排任务顺序。原顺序是「账号A 发完全部笔记 → 账号B 再发全部笔记」，
    # 跑矩阵时同一批内容会在很短的窗口里集中出现在多个账号上，这是最典型的
    # 矩阵特征。纯随机打乱还不够：同一素材仍可能连着落到不同账号。
    # 这里用贪心，尽量保证相邻两条「素材不同、账号也不同」，
    # 让同一条笔记落到各账号的时间被摊到最开。
    pool = [(a, m) for a in account_names for m in picked]
    random.shuffle(pool)
    tasks: list[tuple[str, dict]] = []
    last_material = last_account = None
    while pool:
        pick = next(
            (
                i
                for i, (a, m) in enumerate(pool)
                if m.get("id") != last_material and a != last_account
            ),
            None,
        )
        if pick is None:
            pick = next(
                (i for i, (_a, m) in enumerate(pool) if m.get("id") != last_material),
                0,
            )
        account, material = pool.pop(pick)
        tasks.append((account, material))
        last_material, last_account = material.get("id"), account

    auths: dict[str, Any] = {}   # 复用登录态，避免每条都重新 bootstrap
    apis: dict[str, Any] = {}

    def close_account(name: str) -> None:
        auth = auths.pop(name, None)
        apis.pop(name, None)
        if auth is not None:
            try:
                auth.close()
            except Exception:
                pass

    try:
        for name, material in tasks:
            if _job_cancelled(job_id):
                break
            title = material.get("title", "")
            try:
                if name not in apis:
                    auths[name] = build_auth(name)
                    apis[name] = XHS_Creator_Apis(auths[name].creator).bootstrap()
                ok, message, _ = apis[name].post_note(
                    material_to_note(material, privacy, post_time)
                )
                record(name, title, ok, message)
            except Exception as exc:
                record(name, title, False, str(exc))
                # 只有登录态真的坏了才丢弃会话，其它错误（比如内容被拒）不影响后续
                if classify_account_error(exc)[0] == "expired":
                    close_account(name)
            if not wait_between():
                break
    except Exception as exc:
        traceback.print_exc()
        _job_patch(job_id, status="error", message=str(exc), next_at=0)
        return
    finally:
        for name in list(auths):
            close_account(name)

    cancelled = _job_cancelled(job_id)
    _job_patch(
        job_id,
        status="cancelled" if cancelled else "done",
        finished_at=time.time(),
        next_at=0,
        results=list(results),
        done=len(results),
        succeeded=sum(1 for r in results if r["ok"]),
    )


@app.post("/api/publish/batch")
def api_publish_batch(payload: dict) -> Any:
    account_names = payload.get("accounts") or []
    material_ids = payload.get("materials") or []
    privacy = int(payload.get("privacy", 1) or 1)
    if not account_names:
        return err("请至少勾选一个账号")
    if not material_ids:
        return err("请至少勾选一条笔记")

    try:
        post_time = parse_schedule(payload.get("schedule_at") or "")
    except ValueError as exc:
        return err(str(exc))

    try:
        delay_min = float(payload.get("delay_min", DEFAULT_DELAY_MIN))
        delay_max = float(payload.get("delay_max", DEFAULT_DELAY_MAX))
    except (TypeError, ValueError):
        return err("间隔时间必须是数字")
    delay_min = max(0.0, delay_min)
    delay_max = max(delay_min, delay_max)

    pool = {m.get("id"): m for m in load_library()}
    picked = [pool[i] for i in material_ids if i in pool]
    if not picked:
        return err("勾选的笔记在素材库里已不存在")

    job_id = f"{int(time.time() * 1000):x}"
    with _PUBLISH_LOCK:
        PUBLISH_JOBS[job_id] = {
            "id": job_id,
            "status": "running",
            "total": len(account_names) * len(picked),
            "done": 0,
            "succeeded": 0,
            "results": [],
            "message": "",
            "cancel": False,
            "started_at": time.time(),
            "finished_at": 0,
            "next_at": 0,
            "delay_min": delay_min,
            "delay_max": delay_max,
        }
        # 只留最近若干个任务
        if len(PUBLISH_JOBS) > PUBLISH_JOB_KEEP:
            for old in sorted(PUBLISH_JOBS, key=lambda k: PUBLISH_JOBS[k]["started_at"])[
                : len(PUBLISH_JOBS) - PUBLISH_JOB_KEEP
            ]:
                PUBLISH_JOBS.pop(old, None)

    threading.Thread(
        target=_publish_worker,
        args=(job_id, list(account_names), picked, privacy, post_time, delay_min, delay_max),
        daemon=True,
    ).start()
    return {"ok": True, "job": dict(PUBLISH_JOBS[job_id])}


@app.get("/api/publish/job/{job_id}")
def api_publish_job(job_id: str) -> Any:
    with _PUBLISH_LOCK:
        job = PUBLISH_JOBS.get(job_id)
        return {"ok": True, "job": dict(job)} if job else err("任务不存在", 404)


@app.post("/api/publish/job/{job_id}/cancel")
def api_publish_job_cancel(job_id: str) -> Any:
    with _PUBLISH_LOCK:
        job = PUBLISH_JOBS.get(job_id)
        if not job:
            return err("任务不存在", 404)
        job["cancel"] = True
    return {"ok": True}


# --------------------------------------------------------------------------- #
# 私信
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# 已发布笔记（查看 / 删除）
#
# 删除接口不在上游仓库里，是从创作者平台的前端 bundle 里翻出来的：
#   DELETE_NOTE = `${creator}/web_api/sns/capa/postgw/note/delete`
# 请求体只需要 note_id（实测传 note_ids / id 都会被 400 挡回）。
# --------------------------------------------------------------------------- #


def _note_rows(payload: Any) -> list[dict]:
    rows = (
        payload
        if isinstance(payload, list)
        else ((payload or {}).get("data") or {}).get("notes") or []
    )
    out: list[dict] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        images = item.get("images_list") or []
        cover = images[0].get("url") if images and isinstance(images[0], dict) else ""
        out.append(
            {
                "id": str(item.get("id") or ""),
                "title": item.get("display_title") or "(无标题)",
                "cover": cover or "",
                "likes": item.get("likes") or 0,
                "comments": item.get("comments_count") or 0,
                "views": item.get("view_count") or 0,
                "time": item.get("time") or 0,
                "sticky": bool(item.get("sticky")),
                "permission": item.get("permission_msg") or "",
            }
        )
    return out


@app.get("/api/notes")
def api_notes(name: str) -> Any:
    try:
        from apis.xhs_creator_apis import XHS_Creator_Apis

        auth = build_auth(name)
        try:
            api = XHS_Creator_Apis(auth.creator).bootstrap()
            success, message, notes = api.get_all_posted_notes()
        finally:
            auth.close()
        return {
            "ok": True,
            "notes": _note_rows(notes),
            "message": "" if success else str(message),
        }
    except Exception as exc:
        traceback.print_exc()
        return err(str(exc), 500)


@app.post("/api/notes/delete")
def api_note_delete(payload: dict) -> Any:
    """删除已发布笔记，支持一次跨多个账号。

    新格式：{"items": [{"name": "main", "note_id": "..."}, ...]}
    同时兼容旧的 {"name": ..., "note_ids": [...]} / {"name": ..., "note_id": ...}
    """
    raw_items = payload.get("items")
    if not raw_items:
        name = (payload.get("name") or "").strip()
        ids = payload.get("note_ids")
        if ids is None:
            single = (payload.get("note_id") or "").strip()
            ids = [single] if single else []
        raw_items = [{"name": name, "note_id": i} for i in ids]

    items: list[tuple[str, str]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        note_id = str(item.get("note_id") or "").strip()
        if name and note_id:
            items.append((name, note_id))
    if not items:
        return err("没有要删除的笔记")

    # 按账号分组：同一个账号只登录一次
    grouped: dict[str, list[str]] = {}
    for name, note_id in items:
        grouped.setdefault(name, []).append(note_id)

    try:
        from apis.xhs_creator_apis import XHS_Creator_Apis

        results: list[dict] = []
        for name, note_ids in grouped.items():
            try:
                auth = build_auth(name)
            except Exception as exc:
                for note_id in note_ids:
                    results.append(
                        {"account": name, "note_id": note_id, "ok": False, "message": str(exc)}
                    )
                continue
            try:
                api = XHS_Creator_Apis(auth.creator).bootstrap()
                for index, note_id in enumerate(note_ids):
                    try:
                        success, message, _ = api.delete_note(note_id)
                        results.append(
                            {
                                "account": name,
                                "note_id": note_id,
                                "ok": bool(success),
                                "message": str(message),
                            }
                        )
                    except Exception as exc:
                        results.append(
                            {"account": name, "note_id": note_id, "ok": False, "message": str(exc)}
                        )
                    if index < len(note_ids) - 1:
                        time.sleep(random.uniform(1.5, 3.0))   # 别连着猛点删除
            finally:
                auth.close()
        succeeded = sum(1 for r in results if r["ok"])
        return {
            "ok": True,
            "results": results,
            "succeeded": succeeded,
            "total": len(results),
        }
    except Exception as exc:
        traceback.print_exc()
        return err(str(exc), 500)


@app.get("/api/dm/chats")
def api_dm_chats(name: str, limit: int = 50) -> Any:
    try:
        from apis.xhs_live import XHSLiveAPI

        auth = build_auth(name)
        try:
            payload = XHSLiveAPI(auth.pc).get_chats(limit=limit)
        finally:
            auth.close()
        data = (payload or {}).get("data") or {}
        rows = []
        for row in data.get("chats") or []:
            info = row.get("info") or {}
            when = row.get("last_msg_time")
            rows.append(
                {
                    "user_id": row.get("chat_user_id") or "",
                    "nickname": info.get("nickname") or info.get("user_name") or "(无昵称)",
                    "avatar": info.get("avatar") or "",
                    "last": row.get("last_msg_content") or "",
                    "time": when or 0,
                }
            )
        return {"ok": True, "chats": rows}
    except Exception as exc:
        traceback.print_exc()
        return err(str(exc), 500)


@app.get("/api/dm/history")
def api_dm_history(name: str, user_id: str, limit: int = 30) -> Any:
    try:
        from apis.xhs_live import XHSLiveAPI

        auth = build_auth(name)
        try:
            me = getattr(auth.pc_auth, "user_id", "") or ""
            payload = XHSLiveAPI(auth.pc).get_message_history(user_id, limit=limit)
        finally:
            auth.close()
        data = (payload or {}).get("data") or {}
        rows = []
        for row in reversed(data.get("out_message_list") or []):
            text = row.get("content")
            if isinstance(text, str):
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, dict):
                        text = parsed.get("content", text)
                except (TypeError, json.JSONDecodeError):
                    pass
            rows.append(
                {
                    "mine": row.get("sender_id") == me,
                    "text": text if isinstance(text, str) else json.dumps(text, ensure_ascii=False),
                    "time": row.get("created_at") or 0,
                    "revoked": bool(row.get("revoked")),
                }
            )
        return {"ok": True, "messages": rows}
    except Exception as exc:
        traceback.print_exc()
        return err(str(exc), 500)


def _mark_read_sync(live, user_id: str | None) -> dict:
    """把某个会话（user_id 为 None 时=全部未读会话）标记为已读。

    已读接口的 ``chat_id`` 就是对方的 user_id，``read_store_id`` 取
    会话列表里的 ``max_store_id``。这两个字段在 ``get_chats`` 的返回里没有
    直接对应名，实测确认后固化在这里。
    """
    data = (live.get_chats(limit=50) or {}).get("data") or {}
    counts = ((live.get_unread() or {}).get("data") or {}).get("user_chat_unread_counts") or {}
    total = sum(int(v or 0) for v in counts.values())

    chat_list = []
    for row in data.get("chats") or []:
        uid = row.get("chat_user_id") or ""
        unread = int(counts.get(uid) or 0)
        if unread <= 0:
            continue
        if user_id is not None and uid != user_id:
            continue
        chat_list.append(
            {
                "chat_id": uid,
                "read_store_id": row.get("max_store_id") or 0,
                "unread_count": unread,
                "type": 1,
                "need_rm_offline": True,
            }
        )

    if not chat_list:
        return {"cleared": 0, "skipped": True}

    cleared = sum(item["unread_count"] for item in chat_list)
    result = live.mark_messages_read(
        chat_list, chat_total_unread_count=max(0, total - cleared)
    )
    return {"cleared": cleared, "result": result}


def _reset_monitor_account(name: str) -> None:
    """账号刚登录/覆盖/改名后，立刻清掉监控里的旧状态。

    扫码登录流程最后一步会验证正式会话（guest 必须为 False），所以此刻
    登录态确定是有效的。如果不清，界面会继续显示上一轮的「需要重新登录」，
    直到下一轮抓取（最多十几秒）才更新 —— 用户会以为覆盖没生效，反复重登。
    """
    _MONITOR_AUTH.pop(name, None)
    with _MONITOR_LOCK:
        info = _MONITOR["accounts"].get(name)
        if info is not None:
            info.update({"error": "", "error_kind": "", "error_count": 0})
            _MONITOR["updated_at"] = time.time()
    _MONITOR_WAKE.set()  # 让监控线程立刻重抓一次


def _clear_monitor_unread(name: str, user_id: str | None, cleared: int) -> None:
    """把监控缓存里的未读同步清零，前端不用等下一轮就有反馈。"""
    with _MONITOR_LOCK:
        state = _MONITOR["accounts"].get(name)
        if not state:
            return
        for chat in state.get("chats") or []:
            if user_id is not None and chat.get("user_id") != user_id:
                continue
            chat["unread"] = 0
        state["unread_total"] = max(0, int(state.get("unread_total") or 0) - cleared)
        _MONITOR["updated_at"] = time.time()


@app.post("/api/dm/read")
def api_dm_read(payload: dict) -> Any:
    name = payload.get("name")
    user_id = payload.get("user_id")
    if not name:
        return err("缺少账号")
    try:
        from apis.xhs_live import XHSLiveAPI

        auth = build_auth(name)
        try:
            result = _mark_read_sync(XHSLiveAPI(auth.pc), user_id)
        finally:
            auth.close()
        if result.get("cleared"):
            _clear_monitor_unread(name, user_id, result["cleared"])
        return {"ok": True, **result}
    except Exception as exc:
        traceback.print_exc()
        return err(str(exc), 500)


@app.post("/api/dm/read-all")
def api_dm_read_all(payload: dict) -> Any:
    name = payload.get("name")
    if not name:
        return err("缺少账号")
    try:
        from apis.xhs_live import XHSLiveAPI

        auth = build_auth(name)
        try:
            result = _mark_read_sync(XHSLiveAPI(auth.pc), None)
        finally:
            auth.close()
        if result.get("cleared"):
            _clear_monitor_unread(name, None, result["cleared"])
        return {"ok": True, **result}
    except Exception as exc:
        traceback.print_exc()
        return err(str(exc), 500)


@app.post("/api/dm/send")
def api_dm_send(payload: dict) -> Any:
    name = payload.get("name")
    user_id = payload.get("user_id")
    text = payload.get("text")
    if not (name and user_id and text):
        return err("缺少参数")
    try:
        from apis.xhs_live import XHSLiveAPI

        auth = build_auth(name)

        async def send() -> dict:
            live = XHSLiveAPI(auth.pc)
            websocket = await live.connect_push_from_storage()
            try:
                return await websocket.send_private_message(user_id, text)
            finally:
                await websocket.close()

        try:
            sent = asyncio.run(send())
        finally:
            auth.close()
        return {"ok": True, "mid": (sent or {}).get("mid", "")}
    except Exception as exc:
        traceback.print_exc()
        return err(str(exc), 500)


# --------------------------------------------------------------------------- #
# 监控实现
# --------------------------------------------------------------------------- #


def account_names() -> list[str]:
    if not ACCOUNT_DIR.is_dir():
        return []
    return sorted(p.stem for p in ACCOUNT_DIR.glob("*.json"))


def account_nickname(name: str) -> str:
    try:
        return load_account(name).get("nickname") or name
    except Exception:
        return name


def last_message_sender(live, chat_user_id: str) -> str:
    """查某个会话最后一条消息是谁发的。

    ``last_msg_time`` 对「别人发来的」和「自己发出去的」都会变，
    所以未读没涨的情况下必须核实发送者，否则你回一条消息也会被当成新私信提醒。
    """
    try:
        payload = live.get_message_history(chat_user_id, limit=1)
        rows = ((payload or {}).get("data") or {}).get("out_message_list") or []
        if rows:
            return str(rows[0].get("sender_id") or "")
    except Exception:
        pass
    return ""


def _monitor_auth(name: str) -> XHSUnifiedAuth:
    """监控线程专用：复用已建立的会话，避免每轮都跑一次 bootstrap。"""
    auth = _MONITOR_AUTH.get(name)
    if auth is None:
        auth = build_auth(name)
        _MONITOR_AUTH[name] = auth
    return auth


def classify_account_error(exc: Exception) -> tuple[str, str]:
    """把抓取异常归类，决定界面该说「重新登录」还是「网络异常」。

    返回 (kind, 人话)。kind 取值：
      expired  登录态失效，需要重新扫码
      network  网络问题，下一轮会自动重试
      env      运行环境问题（例如 curl_cffi 版本过低），重登也没用
      unknown  其它错误
    """
    text = f"{type(exc).__name__}: {exc}"
    low = text.lower()

    # 注意是 "impersonat"：报错原文是 "Impersonating chrome150 is not supported"，
    # 里面并不包含 "impersonate"
    if "impersonat" in low:
        return "env", "运行环境问题：curl_cffi 版本过低（需 >=0.16.2），重新登录也没用"

    # 失效的真实形态：
    #   bootstrap user/me failed: 登录已过期        <- 会话过期
    #   bootstrap user/me failed: JS mnsv2 签名失败  <- Cookie 已无效导致签名过不了门禁
    # build_auth() 也会统一包一层「登录态已失效」，这里一并认掉
    if any(
        k in low
        for k in (
            "bootstrap", "user/me", "guest",
            "登录已过期", "登录态已失效", "未返回 user_id",
        )
    ):
        return "expired", "登录态已失效，需要重新扫码登录"

    if any(
        k in low
        for k in (
            "timeout", "timed out", "connection", "connect", "ssl",
            "proxy", "resolve", "network", "temporarily",
        )
    ):
        return "network", "网络异常，稍后会自动重试"

    return "unknown", f"{type(exc).__name__}: {str(exc)[:160]}"


def _fetch_account_state(name: str) -> dict:
    """读一个账号的未读数 + 会话列表，合成前端要的结构。"""
    from apis.xhs_live import XHSLiveAPI

    live = XHSLiveAPI(_monitor_auth(name).pc)

    unread_payload = live.get_unread()
    counts = ((unread_payload or {}).get("data") or {}).get("user_chat_unread_counts") or {}

    chats_payload = live.get_chats(limit=50)
    data = (chats_payload or {}).get("data") or {}
    rows = []
    for row in data.get("chats") or []:
        info = row.get("info") or {}
        uid = row.get("chat_user_id") or ""
        rows.append(
            {
                "user_id": uid,
                "nickname": info.get("nickname") or info.get("user_name") or "(无昵称)",
                "avatar": info.get("avatar") or "",
                "last": row.get("last_msg_content") or "",
                "time": row.get("last_msg_time") or 0,
                "unread": int(counts.get(uid) or 0),
                "store": row.get("max_store_id") or 0,
            }
        )
    return {
        "unread_total": sum(int(v or 0) for v in counts.values()),
        "unread": {k: int(v or 0) for k, v in counts.items()},
        "chats": rows,
        "updated_at": time.time(),
        "error": "",
        "error_kind": "",
        "error_count": 0,
    }


def _monitor_loop() -> None:
    while True:
        with _MONITOR_LOCK:
            if not _MONITOR["running"]:
                return
            interval = int(_MONITOR["interval"])

        names = account_names()
        with _MONITOR_LOCK:
            known = set(_MONITOR["accounts"])
            for gone in known - set(names):
                _MONITOR["accounts"].pop(gone, None)

        for name in names:
            with _MONITOR_LOCK:
                if not _MONITOR["running"]:
                    return
                prev = _MONITOR["accounts"].get(name) or {}
            try:
                state = _fetch_account_state(name)
            except Exception as exc:
                _MONITOR_AUTH.pop(name, None)  # 会话可能过期，丢掉下次重建
                kind, human = classify_account_error(exc)
                # 连续失败两次以上才当成真的失效，避免网络抖一下就误报要重新登录
                fails = int((prev.get("error_count") or 0) if prev else 0) + 1
                if fails < 2 and kind in ("expired", "network", "unknown"):
                    kind = "retry"
                    human = f"本次抓取失败，正在重试（{human}）"
                state = {
                    "unread_total": 0,
                    "unread": {},
                    "chats": [],
                    "updated_at": time.time(),
                    "error": human,
                    "error_kind": kind,
                    "error_count": fails,
                }

            # 新消息判定：未读变多，或最后消息时间前进。
            # 只看未读会漏掉「消息到了但你已经在 App 里读过」的情况。
            # 首轮没有 prev，不报，避免把历史消息一次性刷屏。
            if prev:
                prev_unread = {c.get("user_id"): c.get("unread", 0) for c in (prev.get("chats") or [])}
                prev_time = {c.get("user_id"): c.get("time", 0) for c in (prev.get("chats") or [])}
                nick = account_nickname(name)
                live = None
                my_uid = ""
                for chat in state.get("chats") or []:
                    uid = chat.get("user_id")
                    known = uid in prev_unread or uid in prev_time
                    if known:
                        unread_up = chat.get("unread", 0) > prev_unread.get(uid, 0)
                        is_new = unread_up or chat.get("time", 0) > prev_time.get(uid, 0)
                    else:
                        # 之前没见过这个会话，说明是新的对话
                        unread_up = chat.get("unread", 0) > 0
                        is_new = bool(chat.get("time", 0))
                    if not is_new:
                        continue
                    if not unread_up:
                        # 未读没涨但时间前进：可能是自己刚发出去的，核实发送者
                        if live is None:
                            from apis.xhs_live import XHSLiveAPI

                            live = XHSLiveAPI(_monitor_auth(name).pc)
                            my_uid = str(getattr(_monitor_auth(name).pc, "user_id", "") or "")
                        if last_message_sender(live, uid) == my_uid:
                            continue
                    _push_event(name, nick, chat)

            with _MONITOR_LOCK:
                _MONITOR["accounts"][name] = state
                _MONITOR["updated_at"] = time.time()

        # 分片睡眠，这样改间隔或停止能很快生效
        for _ in range(max(1, interval)):
            if _MONITOR_WAKE.wait(1.0):
                _MONITOR_WAKE.clear()
                break
            with _MONITOR_LOCK:
                if not _MONITOR["running"]:
                    return


def start_monitor() -> None:
    global _MONITOR_THREAD
    with _MONITOR_LOCK:
        already = _MONITOR_THREAD is not None and _MONITOR_THREAD.is_alive()
        _MONITOR["running"] = True
    if already:
        return
    _MONITOR_THREAD = threading.Thread(target=_monitor_loop, daemon=True)
    _MONITOR_THREAD.start()


@app.get("/api/monitor/state")
def api_monitor_state() -> Any:
    start_monitor()  # 第一次访问自动拉起
    with _MONITOR_LOCK:
        return {
            "ok": True,
            "running": _MONITOR["running"],
            "interval": _MONITOR["interval"],
            "updated_at": _MONITOR["updated_at"],
            "accounts": json.loads(json.dumps(_MONITOR["accounts"], ensure_ascii=False)),
        }


@app.post("/api/monitor/config")
def api_monitor_config(payload: dict) -> Any:
    interval = payload.get("interval")
    running = payload.get("running")
    with _MONITOR_LOCK:
        if interval is not None:
            _MONITOR["interval"] = max(5, min(600, int(interval)))
        if running is not None:
            _MONITOR["running"] = bool(running)
    if running:
        start_monitor()
    _MONITOR_WAKE.set()
    return {"ok": True, "interval": _MONITOR["interval"], "running": _MONITOR["running"]}


@app.post("/api/monitor/refresh")
def api_monitor_refresh() -> Any:
    start_monitor()
    _MONITOR_WAKE.set()
    return {"ok": True}


@app.get("/api/settings/dingtalk")
def api_dingtalk_get() -> Any:
    return {"ok": True, "dingtalk": load_settings().get("dingtalk", {})}


@app.post("/api/settings/dingtalk")
def api_dingtalk_save(payload: dict) -> Any:
    data = load_settings()
    cfg = data.setdefault("dingtalk", {})
    for key in ("enabled", "webhook", "secret", "keyword", "at_mobiles", "at_all", "accounts"):
        if key in payload:
            cfg[key] = payload[key]
    cfg["enabled"] = bool(cfg.get("enabled"))
    cfg["at_all"] = bool(cfg.get("at_all"))
    save_settings(data)
    return {"ok": True, "dingtalk": cfg}


@app.post("/api/settings/dingtalk/test")
def api_dingtalk_test(payload: dict | None = None) -> Any:
    cfg = dict(load_settings().get("dingtalk", {}))
    for key, value in (payload or {}).items():   # 允许用还没保存的内容直接测
        if key in cfg:
            cfg[key] = value
    if not (cfg.get("webhook") or "").strip():
        return err("请先填写 Webhook 地址")
    body = (
        "### 小红书私信提醒 · 测试\n\n"
        "- **账号**：示例账号（main）\n"
        "- **来自**：示例用户\n"
        "- **内容**：这是一条测试消息，收到说明配置成功\n"
        "- **未读**：1 条\n"
    )
    ok, message = send_dingtalk("小红书新私信（测试）", body, cfg)
    if not ok:
        return err(message)
    return {"ok": True, "message": message}


@app.get("/api/monitor/events")
def api_monitor_events(since: int = 0):
    """SSE 推送新消息事件。

    后台标签页会把 setInterval 限流到分钟级，用长连接推送才能保证
    提示音和弹窗及时（浏览器对 SSE 的节流远小于定时器）。
    """
    start_monitor()

    def stream():
        last = since
        while True:
            with _EVENTS_LOCK:
                fresh = [dict(e) for e in _EVENTS if e["id"] > last]
            for event in fresh:
                last = event["id"]
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            yield ": keep-alive\n\n"
            time.sleep(2)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# --------------------------------------------------------------------------- #
# 启动：只监听「本机回环 + Tailscale 地址」
#
# 本机 Windows 防火墙三个配置文件都是关闭的，靠防火墙拦局域网并不可靠。
# 直接不绑 0.0.0.0，从 socket 层就杜绝同一局域网/公网访问；
# 能连进来的只剩本机和已加入你 tailnet 的设备。
# --------------------------------------------------------------------------- #

LISTEN_PORT = 8848


def _is_tailscale_ip(ip: str) -> bool:
    """Tailscale 用 CGNAT 段 100.64.0.0/10。"""
    try:
        first, second = (int(part) for part in ip.split(".")[:2])
    except (ValueError, IndexError):
        return False
    return first == 100 and 64 <= second <= 127


def find_tailscale_ip() -> str | None:
    """优先问 tailscale CLI，问不到就从 ipconfig 里按网段找。"""
    candidates = [r"C:\Program Files\Tailscale\tailscale.exe", "tailscale"]
    for exe in candidates:
        try:
            proc = subprocess.run(
                [exe, "ip", "-4"], capture_output=True, text=True, timeout=8
            )
        except Exception:
            continue
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                ip = line.strip()
                if ip and _is_tailscale_ip(ip):
                    return ip
    try:
        proc = subprocess.run(["ipconfig"], capture_output=True, text=True, timeout=8)
        for line in proc.stdout.splitlines():
            match = re.search(r"IPv4[^:]*:\s*(\d+\.\d+\.\d+\.\d+)", line)
            if match and _is_tailscale_ip(match.group(1)):
                return match.group(1)
    except Exception:
        pass
    return None


def _bind(host: str) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, LISTEN_PORT))
    sock.set_inheritable(True)
    return sock


def open_browser_when_ready(port: int, timeout: float = 20.0) -> None:
    """等端口真的能建立连接后再打开浏览器。

    固定 sleep 几秒不可靠：机器慢的时候 uvicorn 还没开始 accept，
    浏览器就会先弹出一个「无法访问此网站」。这里改成轮询探测。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.25)
    else:
        print("（服务启动超时，请手动打开上面的地址）")
        return
    try:
        import webbrowser

        webbrowser.open(f"http://127.0.0.1:{port}")
    except Exception as exc:
        print(f"（自动打开浏览器失败：{exc}，请手动打开上面的地址）")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Spider_XHS 本地网页控制台")
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="启动后不要自动打开浏览器",
    )
    run_args = parser.parse_args()

    # 控制台按 UTF-8 输出中文；顺便把窗口标题设成中文
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleTitleW("小红书控制台")
    except Exception:
        pass

    # 已经有实例在跑？那就只打开浏览器，不要再起一个。
    # Windows 的 SO_REUSEADDR 允许两个进程绑定同一端口，第二个实例会
    # 「看起来启动成功」，但请求仍然由第一个进程处理 —— 结果就是你改了代码
    # 却没生效，或者关掉一个窗口服务还在跑，非常难排查。
    try:
        with socket.create_connection(("127.0.0.1", LISTEN_PORT), timeout=0.6):
            print(f"控制台已经在运行了，直接为你打开 http://127.0.0.1:{LISTEN_PORT}")
            try:
                import webbrowser

                webbrowser.open(f"http://127.0.0.1:{LISTEN_PORT}")
            except Exception:
                pass
            raise SystemExit(0)
    except OSError:
        pass

    sockets = []
    try:
        sockets.append(_bind("127.0.0.1"))
    except OSError as exc:
        print(f"绑定 127.0.0.1:{LISTEN_PORT} 失败：{exc}")
        raise SystemExit(1)

    tailscale_ip = find_tailscale_ip()
    if tailscale_ip:
        try:
            sockets.append(_bind(tailscale_ip))
        except OSError as exc:
            print(f"绑定 Tailscale 地址 {tailscale_ip} 失败：{exc}")
            print("（其它设备将无法访问，请确认 Tailscale 已登录并处于连接状态）")

    print("Spider_XHS 控制台已启动")
    print(f"  本机访问        ：http://127.0.0.1:{LISTEN_PORT}")
    if tailscale_ip and len(sockets) > 1:
        print(f"  手机 / 其它设备 ：http://{tailscale_ip}:{LISTEN_PORT}")
    else:
        print("  手机 / 其它设备 ：未检测到 Tailscale 地址，当前只有本机可访问")
    print("  （未监听 0.0.0.0，同一局域网内的其它设备无法访问）")

    if run_args.no_browser:
        print("  已指定 --no-browser，不自动打开浏览器")
    else:
        print("  正在启动浏览器...")
        threading.Thread(
            target=open_browser_when_ready, args=(LISTEN_PORT,), daemon=True
        ).start()

    # 服务一起来就开始检查账号，不必等有人打开网页。
    # 否则服务重启后如果没人访问，监控其实一直没跑，钉钉也不会推送。
    start_monitor()
    print(f"  账号检查已启动：后台每 {_MONITOR['interval']} 秒巡检一轮")

    uvicorn.Server(uvicorn.Config(app, log_level="warning")).run(sockets=sockets)
