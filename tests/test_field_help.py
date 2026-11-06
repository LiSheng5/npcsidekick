"""字段说明单一来源的防漂移测试。

唯一手写数据在 `core/field_help.py`：控制台经 `/api/state` 取，
`personas/_角色卡说明.md` 的表格由 `scripts/gen_role_doc.py` 生成。
"""
from __future__ import annotations

from pathlib import Path

from core import field_help

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "personas" / "_角色卡说明.md"
APP_JS = ROOT / "web" / "console" / "app.js"
BEGIN = "<!-- BEGIN FIELD_HELP -->"
END = "<!-- END FIELD_HELP -->"


# ── 单一数据源 ────────────────────────────────────────────

def test_source_is_complete_and_has_no_duplicate_keys():
    keys = [row[0] for row in field_help.FIELDS]
    assert len(keys) == len(set(keys))
    assert {"id", "name", "rules.replies", "rules.fallback", "relations"} <= set(keys)
    assert all(len(row) == 4 and all(part.strip() for part in row)
               for row in field_help.FIELDS)


def test_state_endpoint_delivers_field_help(tmp_store, make_app_client):
    state = make_app_client().get("/api/state").json()
    rows = state["field_help"]
    assert rows == field_help.for_api()
    assert rows and all(set(row) == {"key", "cn", "desc"} for row in rows)
    # 旧前端副本把 id 写成「只能用小写字母」，与控制台校验不符
    assert "含大写" in next(row["desc"] for row in rows if row["key"] == "id")


def test_doc_table_matches_source():
    text = DOC.read_text(encoding="utf-8")
    assert BEGIN in text and END in text
    block = text.split(BEGIN, 1)[1].split(END, 1)[0].strip().replace("\r\n", "\n")
    assert block == field_help.render_markdown_table()


def test_frontend_keeps_no_local_copy():
    js = APP_JS.read_text(encoding="utf-8")
    assert "const FIELD_HELP" not in js
    assert "store.state.field_help" in js


def test_generator_check_passes():
    import subprocess
    import sys

    done = subprocess.run(
        [sys.executable, "scripts/gen_role_doc.py", "--check"],
        cwd=ROOT, capture_output=True,
    )
    assert done.returncode == 0, (done.stdout + done.stderr).decode("utf-8", "replace")
