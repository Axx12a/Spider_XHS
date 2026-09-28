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
    }
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
    return HTMLResponse(html)


# --------------------------------------------------------------------------- #
# 账号 / 登录
# --------------------------------------------------------------------------- #


@app.get("/api/accounts")
def api_accounts() -> Any:
    if not ACCOUNT_DIR.is_dir():
        return {"ok": True, "accounts": []}
    rows = []
    for path in sorted(ACCOUNT_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        rows.append(
            {
                "name": data.get("name", path.stem),
                "nickname": data.get("nickname") or "",
                "user_id": data.get("user_id") or "",
                "saved_at": data.get("saved_at") or 0,
            }
        )
    return {"ok": True, "accounts": rows}


@app.delete("/api/accounts/{name}")
def api_account_delete(name: str) -> Any:
    path = account_path(name)
    if path.is_file():
        path.unlink()
    return {"ok": True}


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

    from apis.xhs_creator_apis import XHS_Creator_Apis

    pool = {m.get("id"): m for m in load_library()}
    picked = [pool[i] for i in material_ids if i in pool]
    if not picked:
        return err("勾选的笔记在素材库里已不存在")

    results: list[dict] = []
    for name in account_names:
        try:
            auth = build_auth(name)
        except Exception as exc:
            for material in picked:
                results.append(
                    {"account": name, "title": material.get("title", ""), "ok": False, "message": str(exc)}
                )
            continue
        try:
            api = XHS_Creator_Apis(auth.creator).bootstrap()
            for material in picked:
                title = material.get("title", "")
                try:
                    ok, message, _ = api.post_note(material_to_note(material, privacy, post_time))
                    results.append(
                        {"account": name, "title": title, "ok": bool(ok), "message": str(message)}
                    )
                except Exception as exc:
                    results.append(
                        {"account": name, "title": title, "ok": False, "message": str(exc)}
                    )
                time.sleep(2)  # 连续发布之间留点间隔，降低风控概率
        finally:
            auth.close()

    succeeded = sum(1 for r in results if r["ok"])
    return {"ok": True, "results": results, "succeeded": succeeded, "total": len(results)}


# --------------------------------------------------------------------------- #
# 私信
# --------------------------------------------------------------------------- #


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
                state = {
                    "unread_total": 0,
                    "unread": {},
                    "chats": [],
                    "updated_at": time.time(),
                    "error": str(exc),
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

    uvicorn.Server(uvicorn.Config(app, log_level="warning")).run(sockets=sockets)
