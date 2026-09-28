# encoding: utf-8
"""临时脚本：核对代码用到的第三方包是否都写进了 requirements.txt。用完即删。"""
import ast
import pathlib
import sys

files = ["webui.py", "xhs_cli.py"]
files += [str(p) for p in pathlib.Path("apis").glob("*.py")]
files += [str(p) for p in pathlib.Path("xhs_utils").rglob("*.py")]

mods: set[str] = set()
for path in files:
    try:
        tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
    except Exception:
        continue
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mods.add(node.module.split(".")[0])

stdlib = set(sys.stdlib_module_names)
local = {"apis", "xhs_utils", "spider", "config", "backend"}
third = sorted(m for m in mods if m not in stdlib and m not in local)

req_low = pathlib.Path("requirements.txt").read_text(encoding="utf-8").lower()
alias = {
    "pil": "pillow",
    "multipart": "python-multipart",
    "cv2": "opencv-python",
    "dotenv": "python-dotenv",
    "execjs": "pyexecjs",
    "yaml": "pyyaml",
    "jose": "python-jose",
}

print("代码用到的第三方模块 -> requirements.txt 里有没有：")
missing = []
for mod in third:
    pkg = alias.get(mod, mod)
    ok = pkg.lower() in req_low
    if not ok:
        missing.append(pkg)
    print(f"  {mod:14} (包名 {pkg:18}) {'有' if ok else '缺 !!!'}")

print()
print("缺失的包：", missing if missing else "无")
