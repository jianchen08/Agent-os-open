"""迁移脚本：将扁平的管道执行记录按根任务分组到子目录。

用法：python scripts/migrate_pipeline_grouping.py

步骤：
1. 扫描 data/tasks/tree_*/ 加载所有任务
2. 遍历 parent_task_id 链计算每个任务的根任务 ID
3. 对有 pipeline_run_id 的任务，建立 pipeline_run_id → root_task_id 映射
4. 扫描任务评估历史中的评估子管道，补充映射
5. 移动 data/pipelines/ 下的扁平文件到对应子目录
6. 合并已有的 _pipeline_root_map.json，写入最终映射文件
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import yaml


def find_project_root() -> Path:
    p = Path(__file__).resolve().parent.parent
    if (p / "data").is_dir():
        return p
    print(f"错误：未找到项目根目录（data/），当前路径 {p}")
    sys.exit(1)


def load_all_tasks(task_dir: Path) -> dict[str, dict]:
    tasks: dict[str, dict] = {}
    for tree_dir in sorted(task_dir.glob("tree_*")):
        if not tree_dir.is_dir():
            continue
        for yaml_file in sorted(tree_dir.glob("*.yaml")):
            try:
                data = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
                if isinstance(data, dict) and "id" in data:
                    tasks[data["id"]] = data
            except Exception as exc:
                print(f"  警告：跳过损坏文件 {yaml_file.name}: {exc}")
    # 兼容旧格式扁平文件
    for yaml_file in sorted(task_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                task_list = data.get("tasks")
                if isinstance(task_list, list):
                    for t in task_list:
                        if isinstance(t, dict) and "id" in t:
                            tasks[t["id"]] = t
                elif "id" in data:
                    tasks[data["id"]] = data
        except Exception:
            pass
    return tasks


def find_root_id(task_id: str, tasks: dict[str, dict]) -> str:
    visited: set[str] = set()
    current_id = task_id
    current = tasks.get(task_id)
    if not current:
        return task_id
    parent = current.get("parent_task_id")
    while parent:
        if parent in visited:
            break
        visited.add(parent)
        parent_task = tasks.get(parent)
        if not parent_task:
            break
        current_id = parent_task["id"]
        parent = parent_task.get("parent_task_id")
    return current_id


def main() -> None:
    project_root = find_project_root()
    task_dir = project_root / "data" / "tasks"
    pipeline_dir = project_root / "data" / "pipelines"

    if not task_dir.exists():
        print("错误：data/tasks/ 目录不存在")
        sys.exit(1)
    if not pipeline_dir.exists():
        print("data/pipelines/ 不存在，无需迁移")
        return

    # 1. 加载所有任务
    print("扫描任务文件...")
    tasks = load_all_tasks(task_dir)
    print(f"  加载了 {len(tasks)} 个任务")

    # 2. 建立 pipeline_run_id → root_task_id 映射
    pipeline_root_map: dict[str, str] = {}

    # 2a. 任务的 pipeline_run_id
    for tid, task in tasks.items():
        pid = task.get("pipeline_run_id")
        if pid:
            root_id = find_root_id(tid, tasks)
            pipeline_root_map[pid] = root_id

    # 2b. 评估历史中的评估子管道
    eval_count = 0
    for tid, task in tasks.items():
        meta = task.get("metadata", {})
        if not isinstance(meta, dict):
            continue
        for entry in meta.get("evaluation_history", []):
            for m in entry.get("metrics", []):
                pid = m.get("pipeline_run_id")
                if pid and pid not in pipeline_root_map:
                    root_id = find_root_id(tid, tasks)
                    pipeline_root_map[pid] = root_id
                    eval_count += 1

    # 2c. 合并已有的映射文件（保留运行时新增的映射）
    map_file = pipeline_dir / "_pipeline_root_map.json"
    if map_file.exists():
        try:
            existing = json.loads(map_file.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                for pid, root_id in existing.items():
                    if pid not in pipeline_root_map:
                        pipeline_root_map[pid] = root_id
        except Exception:
            pass

    print(f"  建立了 {len(pipeline_root_map)} 条映射关系（含 {eval_count} 条评估管道）")

    # 3. 移动文件
    moved = 0
    skipped = 0
    orphan = 0
    for pipeline_file in sorted(pipeline_dir.glob("*.yaml")):
        pid = pipeline_file.stem
        root_id = pipeline_root_map.get(pid)
        if root_id:
            target_dir = pipeline_dir / root_id
            target_dir.mkdir(parents=True, exist_ok=True)
            target_path = target_dir / pipeline_file.name
            shutil.move(str(pipeline_file), str(target_path))
            print(f"  移动: {pipeline_file.name} -> {root_id}/")
            moved += 1
        else:
            print(f"  保留(孤立): {pipeline_file.name}")
            orphan += 1

    # 跳过已在子目录中的文件
    for subdir in pipeline_dir.iterdir():
        if subdir.is_dir():
            skipped += sum(1 for _ in subdir.glob("*.yaml"))

    # 4. 写入映射文件
    map_file = pipeline_dir / "_pipeline_root_map.json"
    map_file.write_text(
        json.dumps(pipeline_root_map, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"\n迁移完成:")
    print(f"  移动文件: {moved}")
    print(f"  孤立文件(保留原位): {orphan}")
    print(f"  映射条目: {len(pipeline_root_map)}")
    print(f"  映射文件: {map_file}")


if __name__ == "__main__":
    main()
