# encoding: utf-8
"""临时脚本：验证 Pollinations 免密钥绘图接入。用完即删。"""
import struct
import webui

print("=== 1) 尺寸解析 ===")
for raw in ("1024x1536", "1024X1536", "1080*1440", "garbage", ""):
    print(f"  {raw!r:12} -> {webui._image_size({'image_size': raw})}")

cfg = webui.ai_config()
print("=== 2) 当前配置 ===")
print("  文案来源:", cfg["text_provider"], "| 绘图来源:", cfg["image_provider"], "| 绘图模型:", cfg["image_model"])

print("=== 3) 真实调用 Pollinations 出图 ===")
try:
    blob = webui._pollinations_image(
        "a cozy autumn coffee cup on a wooden table, soft morning light",
        {"image_size": "768x1024", "image_model": "sana"},
    )
    # JPEG 以 FFD8 开头，PNG 以 89504E47 开头
    kind = "JPEG" if blob[:2] == b"\xff\xd8" else ("PNG" if blob[:4] == b"\x89PNG" else "未知")
    print(f"  成功：{len(blob)} 字节，格式 {kind}")
except Exception as exc:
    print("  失败:", exc)

print("=== 4) 走 ai_make_image 分发（应自动选 Pollinations）===")
try:
    blob2 = webui.ai_make_image("a single red apple", cfg)
    print(f"  成功：{len(blob2)} 字节")
except Exception as exc:
    print("  失败:", exc)
