#!/usr/bin/env python3
"""scene 模块功能验证脚本。

验证场景：
1. scene.list_templates 返回 >=3 个预设模板
2. scene.get_active 初始返回 scene=null
3. scene.create(name='测试场景') → success=true, scene.name='测试场景'
4. scene.list 创建后 count >=1
5. scene.switch 切换活跃场景
6. scene.get_active 切换后返回活跃场景
7. scene.delete 删除场景
8. 错误输入：创建时使用不存在的模板 ID
9. 错误输入：切换不存在的场景
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import tempfile
import traceback

# ---- SDK 导入兼容 patch ----
import agentos_plugin_sdk  # noqa: E402
from agentos_plugin_sdk.plugin import AgentOSPlugin  # noqa: E402

agentos_plugin_sdk.AgentOSPlugin = AgentOSPlugin

BASE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(BASE))
SCENE_DIR = os.path.join(PROJECT_ROOT, "plugins", "shared", "system", "scene")

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    status = "✅ PASS" if ok else "❌ FAIL"
    print(f"  {status}: {name}" + (f" — {detail}" if detail else ""))


def load_server(temp_storage: str):
    """加载 scene/server.py 模块，注入临时存储路径到 persistence。

    scene 的 SceneManager 使用 JSON 文件持久化，存储路径默认 data/scenes。
    为了避免测试数据污染和跨模块干扰，我们创建一个临时目录。
    """
    server_path = os.path.join(SCENE_DIR, "server.py")
    spec = importlib.util.spec_from_file_location("scene_server", server_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def run_tests() -> None:
    print("\n=== scene 模块功能验证 ===\n")

    # 创建临时存储目录
    temp_dir = tempfile.mkdtemp(prefix="scene_verify_")

    try:
        server = load_server(temp_dir)
    except Exception as e:
        record("导入 server.py", False, f"导入失败: {e}")
        traceback.print_exc()
        return

    # 初始化服务 — 使用临时存储
    try:
        from pathlib import Path

        sys.path.insert(0, SCENE_DIR)
        from persistence import ScenePersistence  # noqa: F811

        persistence = ScenePersistence(storage_path=temp_dir)
        server._manager = server.SceneManager.__new__(server.SceneManager)
        server._manager._persistence = persistence
        server._manager._active_scene_id = None
        # 加载已有状态（临时目录应为空）
        server._manager._load_active_state()
        record("on_load 初始化（临时存储）", True, f"storage={temp_dir}")
    except Exception as e:
        record("on_load 初始化（临时存储）", False, str(e))
        traceback.print_exc()
        return

    # ---- 步骤1: list_templates ----
    try:
        result = await server.scene_list_templates()
        assert result["success"] is True
        assert result["count"] >= 3, f"模板数应 >=3, 实际: {result['count']}"
        template_ids = {t["id"] for t in result["templates"]}
        assert "chat_workspace" in template_ids, f"缺少 chat_workspace, 有: {template_ids}"
        assert "media_gallery" in template_ids, f"缺少 media_gallery, 有: {template_ids}"
        assert "dashboard" in template_ids, f"缺少 dashboard, 有: {template_ids}"
        record("scene.list_templates >=3 个模板", True, f"ids={template_ids}")
    except Exception as e:
        record("scene.list_templates >=3 个模板", False, str(e))

    # ---- 步骤2: get_active 初始 null ----
    try:
        result = await server.scene_get_active()
        assert result["success"] is True
        assert result["scene"] is None, f"初始应无活跃场景, 实际: {result['scene']}"
        record("scene.get_active 初始 null", True)
    except Exception as e:
        record("scene.get_active 初始 null", False, str(e))

    # ---- 步骤3: create ----
    created_scene_id = None
    try:
        result = await server.scene_create(name="测试场景", description="功能验证测试")
        assert result["success"] is True, f"创建应成功, 实际: {result}"
        scene = result["scene"]
        assert scene["name"] == "测试场景", f"名称不匹配, 实际: {scene['name']}"
        created_scene_id = scene["id"]
        record("scene.create(name='测试场景')", True, f"id={created_scene_id}")
    except Exception as e:
        record("scene.create(name='测试场景')", False, str(e))
        traceback.print_exc()

    # ---- 步骤4: list 创建后 ----
    try:
        result = await server.scene_list()
        assert result["success"] is True
        assert result["count"] >= 1, f"创建后应 >=1, 实际: {result['count']}"
        record("scene.list 创建后 count>=1", True, f"count={result['count']}")
    except Exception as e:
        record("scene.list 创建后 count>=1", False, str(e))

    # ---- 步骤5: switch 切换活跃 ----
    if created_scene_id:
        try:
            result = await server.scene_switch(scene_id=created_scene_id)
            assert result["success"] is True, f"切换应成功, 实际: {result}"
            assert result["scene"]["id"] == created_scene_id
            record("scene.switch 激活场景", True, f"scene_id={created_scene_id}")
        except Exception as e:
            record("scene.switch 激活场景", False, str(e))

    # ---- 步骤6: get_active 切换后 ----
    if created_scene_id:
        try:
            result = await server.scene_get_active()
            assert result["success"] is True
            assert result["scene"] is not None, "切换后应有活跃场景"
            assert result["scene"]["id"] == created_scene_id
            record("scene.get_active 切换后返回活跃", True)
        except Exception as e:
            record("scene.get_active 切换后返回活跃", False, str(e))

    # ---- 步骤7: delete ----
    if created_scene_id:
        try:
            result = await server.scene_delete(scene_id=created_scene_id)
            assert result["success"] is True, f"删除应成功, 实际: {result}"
            record("scene.delete(scene_id)", True, f"删除 id={created_scene_id}")
        except Exception as e:
            record("scene.delete(scene_id)", False, str(e))

    # ---- 步骤8: 验证删除后 list 减一 ----
    try:
        result = await server.scene_list()
        scene_ids = [s["id"] for s in result["scenes"]]
        assert created_scene_id not in scene_ids, f"删除后不应包含, 实际: {scene_ids}"
        record("scene.delete 后 list 确认删除", True)
    except Exception as e:
        record("scene.delete 后 list 确认删除", False, str(e))

    # ---- 补充场景1: 创建时使用不存在的模板 ----
    try:
        result = await server.scene_create(name="bad_template", template_id="nonexistent_template")
        assert result["success"] is False, f"不存在的模板应失败, 实际: {result}"
        record("错误输入: create(template='nonexistent')", True)
    except Exception as e:
        record("错误输入: create(template='nonexistent')", False, str(e))

    # ---- 补充场景2: 切换不存在的场景 ----
    try:
        result = await server.scene_switch(scene_id="nonexistent-scene-id")
        assert result["success"] is False, f"不存在的场景切换应失败, 实际: {result}"
        record("错误输入: switch('nonexistent')", True)
    except Exception as e:
        record("错误输入: switch('nonexistent')", False, str(e))


def main() -> int:
    asyncio.run(run_tests())
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\n=== scene 结果: {passed}/{total} 通过 ===\n")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
