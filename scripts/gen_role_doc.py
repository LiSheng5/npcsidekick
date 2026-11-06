"""把 `core/field_help.py` 的字段说明同步进 `personas/_角色卡说明.md`。

用法：
  python scripts/gen_role_doc.py           生成 / 更新文档里被标记的表格
  python scripts/gen_role_doc.py --check  只校验；表格过期返回 1（供测试与 CI 用）

文档里只有 BEGIN/END 标记之间的是生成物，其余手写内容不动。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import field_help                       # noqa: E402

DOC = ROOT / "personas" / "_角色卡说明.md"
BEGIN = "<!-- BEGIN FIELD_HELP -->"
END = "<!-- END FIELD_HELP -->"


def _replace(text: str) -> str:
    start = text.find(BEGIN)
    end = text.find(END)
    if start < 0 or end < 0 or end < start:
        raise SystemExit(f"文档缺少生成标记：{DOC}")
    newline = "\r\n" if "\r\n" in text else "\n"
    table = field_help.render_markdown_table().replace("\n", newline)
    block = newline.join([BEGIN, table, END])
    return text[:start] + block + text[end + len(END):]


def main() -> int:
    parser = argparse.ArgumentParser(description="同步角色卡字段说明表格")
    parser.add_argument("--check", action="store_true", help="只校验，不写文件")
    args = parser.parse_args()

    text = DOC.read_bytes().decode("utf-8")
    updated = _replace(text)
    if args.check:
        if updated != text:
            print(f"表格已过期，请运行 python scripts/gen_role_doc.py：{DOC}")
            return 1
        print("字段说明表格与 core/field_help.py 一致")
        return 0
    if updated != text:
        DOC.write_bytes(updated.encode("utf-8"))
        print(f"已更新 {DOC}")
    else:
        print("字段说明表格已是最新")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
