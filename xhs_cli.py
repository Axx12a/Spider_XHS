# encoding: utf-8
"""Spider_XHS 便捷命令行：多账号 / 发布笔记 / 私信查看与回复。

Cookie 保存在 accounts/<账号名>.json，扫码登录一次后可直接复用，
不必每次重新登录。

用法：
    python xhs_cli.py login main
    python xhs_cli.py accounts
    python xhs_cli.py publish main -t "标题" -d "正文" -i 1.jpg 2.jpg
    python xhs_cli.py publish main -t "标题" -d "正文" -v clip.mp4
    python xhs_cli.py publish main -t "标题" -d "正文" -i 1.jpg --at "2026-09-30 18:00"
    python xhs_cli.py dm-list main
    python xhs_cli.py dm-read main <user_id>
    python xhs_cli.py dm-send main <user_id> "在的，稍等"
    python xhs_cli.py dm-watch main

需要代理时给任意命令加 --proxy http://127.0.0.1:7890
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from xhs_utils.xhs_auth import XHSUnifiedAuth

ROOT = Path(__file__).resolve().parent
ACCOUNT_DIR = ROOT / "accounts"


# --------------------------------------------------------------------------- #
# 账号存储
# --------------------------------------------------------------------------- #


def account_path(name: str) -> Path:
    safe = "".join(ch for ch in name if ch.isalnum() or ch in "-_")
    if not safe:
        raise SystemExit("账号名只能包含字母、数字、- 和 _")
    return ACCOUNT_DIR / f"{safe}.json"


def save_account(name: str, cookies: str, **extra) -> Path:
    ACCOUNT_DIR.mkdir(exist_ok=True)
    path = account_path(name)
    payload = {
        "name": name,
        "cookies": cookies,
        "saved_at": int(time.time()),
        **extra,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_account(name: str) -> dict:
    path = account_path(name)
    if not path.is_file():
        raise SystemExit(f"没有找到账号 {name}，先执行：python xhs_cli.py login {name}")
    return json.loads(path.read_text(encoding="utf-8"))


def build_auth(name: str, proxy: str | None) -> XHSUnifiedAuth:
    data = load_account(name)
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        return XHSUnifiedAuth.from_cookie(data["cookies"], proxies=proxies)
    except Exception as exc:  # Cookie 过期是常态，提示重新登录
        raise SystemExit(
            f"账号 {name} 的登录态已失效（{exc}）。\n"
            f"请重新登录：python xhs_cli.py login {name}"
        ) from exc


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #


def cmd_login(args) -> None:
    proxies = {"http": args.proxy, "https": args.proxy} if args.proxy else None
    qr_png = Path(args.qr_png).expanduser().resolve()
    _patch_qr_output(qr_png)
    print(f"正在生成二维码，请用小红书 App 扫码登录账号「{args.name}」...\n")
    auth = XHSUnifiedAuth.from_qrcode_login(show_in_terminal=True, proxies=proxies)
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

        path = save_account(
            args.name,
            auth.pc_auth.cookies,
            nickname=nickname,
            user_id=user_id,
        )
        print(f"\n登录成功：{nickname or '(未取到昵称)'} / user_id={user_id or '(未取到)'}")
        print(f"登录态已保存到 {path}")
    finally:
        auth.close()


def _save_qr_png(url: str, path: Path) -> None:
    import qrcode

    qr = qrcode.QRCode(box_size=10, border=4)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(path))


def _patch_qr_output(png_path: Path) -> None:
    """让登录流程在打终端字符码之外，额外把二维码存成 PNG。

    终端里的半角字符码在部分终端会被换行截断，PNG 更稳。
    """
    from apis.xhs_pc_login_apis import XHSLoginApi

    original_terminal = XHSLoginApi.show_qrcode_terminal

    def patched(url):
        try:
            _save_qr_png(url, png_path)
            print(f"二维码图片：{png_path}")
        except Exception as exc:
            print(f"（二维码存图失败，用终端字符码扫码：{exc}）")
        return original_terminal(url)

    XHSLoginApi.show_qrcode_terminal = staticmethod(patched)


def cmd_accounts(args) -> None:
    if not ACCOUNT_DIR.is_dir():
        print("还没有任何已保存的账号。")
        return
    files = sorted(ACCOUNT_DIR.glob("*.json"))
    if not files:
        print("还没有任何已保存的账号。")
        return
    print(f"{'账号':<14}{'昵称':<20}{'user_id':<26}保存时间")
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(data.get("saved_at", 0)))
        print(
            f"{data.get('name', path.stem):<14}"
            f"{(data.get('nickname') or '-'):<20}"
            f"{(data.get('user_id') or '-'):<26}"
            f"{when}"
        )


def _parse_post_time(value: str | None) -> int | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(value, fmt).timestamp() * 1000)
        except ValueError:
            continue
    raise SystemExit(f"无法识别的时间格式：{value}（示例：2026-09-30 18:00）")


def cmd_publish(args) -> None:
    proxies = {"http": args.proxy, "https": args.proxy} if args.proxy else None
    auth = build_auth(args.name, args.proxy)
    try:
        from apis.xhs_creator_apis import XHS_Creator_Apis

        note: dict = {
            "title": args.title,
            "desc": args.desc,
            "type": args.privacy,
        }
        if args.video:
            video = Path(args.video).expanduser()
            if not video.is_file():
                raise SystemExit(f"视频不存在：{video}")
            note["media_type"] = "video"
            note["video"] = str(video)
        else:
            paths = [Path(p).expanduser() for p in (args.images or [])]
            if not paths:
                raise SystemExit("图文发布至少需要一张图片：-i 图片1 图片2 ...")
            missing = [str(p) for p in paths if not p.is_file()]
            if missing:
                raise SystemExit(f"图片不存在：{missing}")
            note["media_type"] = "image"
            note["images"] = [str(p) for p in paths]

        if args.topics:
            note["topics"] = args.topics
        if args.location:
            note["location"] = args.location
        post_time = _parse_post_time(args.at)
        if post_time:
            note["postTime"] = post_time

        api = XHS_Creator_Apis(auth.creator).bootstrap()
        success, message, result = api.post_note(note, proxies=proxies)
        if success:
            when = f"，定时 {args.at}" if args.at else ""
            print(f"发布成功{when}：{message}")
            if result:
                print(json.dumps(result, ensure_ascii=False, indent=2)[:1200])
        else:
            raise SystemExit(f"发布失败：{message} | {result}")
    finally:
        auth.close()


def _chat_rows(payload) -> list[dict]:
    """把 /api/im/web/v3/chats 的返回拍平成一行一会话。"""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data") or payload
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    for key in ("chats", "chat_list", "list", "message_list"):
        rows = data.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def cmd_dm_list(args) -> None:
    auth = build_auth(args.name, args.proxy)
    try:
        from apis.xhs_live import XHSLiveAPI

        live = XHSLiveAPI(auth.pc)
        payload = live.get_chats(limit=args.limit)
        rows = _chat_rows(payload)
        if not rows:
            print("没有取到会话。原始返回：")
            print(json.dumps(payload, ensure_ascii=False, indent=2)[:1500])
            return
        print(f"{'对方 user_id':<26}{'昵称':<20}最后一条")
        for row in rows:
            other = (
                row.get("chat_user_id")
                or row.get("user_id")
                or (row.get("user_info") or {}).get("user_id", "")
            )
            info = row.get("info") or {}
            nick = info.get("nickname") or info.get("user_name") or ""
            last = row.get("last_msg_content") or ""
            if isinstance(last, dict):
                last = last.get("content") or last.get("text") or ""
            when = row.get("last_msg_time")
            stamp = ""
            if isinstance(when, (int, float)) and when > 0:
                value = when / 1000 if when > 10**11 else when
                stamp = time.strftime("%m-%d %H:%M", time.localtime(value))
            print(f"{str(other):<26}{str(nick):<20}{stamp:<12}{str(last)[:40]}")
    finally:
        auth.close()


def cmd_dm_read(args) -> None:
    auth = build_auth(args.name, args.proxy)
    try:
        from apis.xhs_live import XHSLiveAPI

        live = XHSLiveAPI(auth.pc)
        payload = live.get_message_history(args.user_id, limit=args.limit)
        me = getattr(auth.pc_auth, "user_id", "") or ""
        data = (payload or {}).get("data") or {}
        rows = data.get("out_message_list") or data.get("message_list") or []
        if not rows:
            print("没有取到消息。原始返回：")
            print(json.dumps(payload, ensure_ascii=False, indent=2)[:1500])
            return
        for row in reversed(rows):  # 接口按新→旧返回，倒过来更符合阅读顺序
            text = row.get("content")
            if isinstance(text, str):
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, dict):
                        text = parsed.get("content", text)
                except (TypeError, json.JSONDecodeError):
                    pass
            who = "我" if row.get("sender_id") == me else "对方"
            when = row.get("created_at")
            stamp = ""
            if isinstance(when, (int, float)) and when > 0:
                value = when / 1000 if when > 10**11 else when
                stamp = time.strftime("%m-%d %H:%M", time.localtime(value))
            flag = "（已撤回）" if row.get("revoked") else ""
            print(f"[{stamp}] {who}{flag}: {text}")
    finally:
        auth.close()


def cmd_dm_send(args) -> None:
    auth = build_auth(args.name, args.proxy)

    async def run() -> None:
        from apis.xhs_live import XHSLiveAPI

        live = XHSLiveAPI(auth.pc)
        websocket = await live.connect_push_from_storage()
        try:
            sent = await websocket.send_private_message(args.user_id, args.text)
            print(f"已发送给 {args.user_id}：{args.text}（mid={sent.get('mid')}）")
        finally:
            await websocket.close()

    try:
        asyncio.run(run())
    finally:
        auth.close()


def cmd_dm_watch(args) -> None:
    auth = build_auth(args.name, args.proxy)

    async def run() -> None:
        from apis.xhs_live import XHSLiveAPI

        live = XHSLiveAPI(auth.pc)
        websocket = await live.connect_push_from_storage()
        print(f"开始监听账号「{args.name}」的新私信，Ctrl+C 结束...")
        try:
            async for event in websocket.events():
                decoded = (event or {}).get("decoded") or {}
                im = decoded.get("im")
                if not im:
                    continue
                for entry in im if isinstance(im, list) else [im]:
                    if isinstance(entry, dict) and "chatMessage" in entry:
                        message = entry["chatMessage"]
                        print(
                            f"[{time.strftime('%H:%M:%S')}] "
                            f"{message.get('sender', '?')}: "
                            f"{json.dumps(message.get('payload_json'), ensure_ascii=False)[:200]}"
                        )
        finally:
            await websocket.close()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\n已停止监听。")
    finally:
        auth.close()


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Spider_XHS 便捷命令行：多账号 / 发布笔记 / 私信",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--proxy", help="HTTP 代理，例如 http://127.0.0.1:7890")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("login", help="扫码登录并把登录态保存到 accounts/")
    p.add_argument("name")
    p.add_argument(
        "--qr-png",
        default=str(ROOT / "qr_login.png"),
        help="同时把登录二维码存成 PNG（默认 ./qr_login.png）",
    )
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("accounts", help="列出已保存的账号")
    p.set_defaults(func=cmd_accounts)

    p = sub.add_parser("publish", help="发布图文或视频笔记")
    p.add_argument("name")
    p.add_argument("-t", "--title", required=True)
    p.add_argument("-d", "--desc", required=True)
    p.add_argument("-i", "--images", nargs="+", help="图片路径，可多张")
    p.add_argument("-v", "--video", help="视频路径（与 -i 二选一）")
    p.add_argument("--topics", nargs="+", help="话题标签，不带 #")
    p.add_argument("--location", help="地点名称")
    p.add_argument("--at", help='定时发布时间，例如 "2026-09-30 18:00"')
    p.add_argument("--privacy", type=int, default=1, help="可见性，默认 1")
    p.set_defaults(func=cmd_publish)

    p = sub.add_parser("dm-list", help="私信会话列表")
    p.add_argument("name")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_dm_list)

    p = sub.add_parser("dm-read", help="某个会话的消息记录")
    p.add_argument("name")
    p.add_argument("user_id")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=cmd_dm_read)

    p = sub.add_parser("dm-send", help="给某人发私信")
    p.add_argument("name")
    p.add_argument("user_id")
    p.add_argument("text")
    p.set_defaults(func=cmd_dm_send)

    p = sub.add_parser("dm-watch", help="实时监听新私信")
    p.add_argument("name")
    p.set_defaults(func=cmd_dm_watch)

    return parser


def main() -> None:
    # Windows 控制台默认不是 UTF-8，中文会变乱码；能改就改成 UTF-8。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    args = build_parser().parse_args()
    try:
        args.func(args)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
