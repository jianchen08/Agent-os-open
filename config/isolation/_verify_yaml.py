"""临时验证脚本 - 验证 isolation_config.yaml 格式正确性"""
import yaml
import sys

yaml_path = r"d:\Jianguoyun\Agent os\config\isolation\isolation_config.yaml"

try:
    with open(yaml_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    ws = data.get("workspace", {})

    print("=== workspace 段验证 ===")
    print(f"root: {ws.get('root')}")
    print(f"nesting_mode: {ws.get('nesting_mode')}")
    print(f"sparse_threshold_mb: {ws.get('sparse_threshold_mb')}")
    print(f"worktree_include_patterns: {ws.get('worktree_include_patterns')}")
    exc_count = len(ws.get("worktree_exclude_patterns", []))
    print(f"worktree_exclude_patterns count: {exc_count}")
    print(f"large_file_threshold_mb: {ws.get('large_file_threshold_mb')}")
    print(f"worktree_link_patterns: {ws.get('worktree_link_patterns')}")
    print(f"scenario_detection: {ws.get('scenario_detection')}")
    print(f"new_project: {ws.get('new_project')}")
    print(f"existing_project: {ws.get('existing_project')}")
    print(f"max_eval_retries: {ws.get('max_eval_retries')}")

    print()
    print("=== 其他段完整性检查 ===")
    print(f"coordinator.enabled: {data.get('coordinator', {}).get('enabled')}")
    providers = data.get("providers", {})
    print(f"providers.host.enabled: {providers.get('host', {}).get('enabled')}")
    print(f"lifecycle.project_ttl: {data.get('lifecycle', {}).get('project_ttl')}")

    # 验收标准检查
    errors = []
    if ws.get("sparse_threshold_mb") != 500:
        errors.append("sparse_threshold_mb 应为 500")
    if ws.get("large_file_threshold_mb") != 50:
        errors.append("large_file_threshold_mb 应为 50")
    if ws.get("max_eval_retries") != 3:
        errors.append("max_eval_retries 应为 3")
    if not ws.get("worktree_include_patterns"):
        errors.append("worktree_include_patterns 缺失")
    if not ws.get("worktree_exclude_patterns"):
        errors.append("worktree_exclude_patterns 缺失")
    if not ws.get("worktree_link_patterns"):
        errors.append("worktree_link_patterns 缺失")

    print()
    if errors:
        print("验收失败:")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    else:
        print("YAML 解析成功! 所有配置项完整，验收通过。")

except yaml.YAMLError as e:
    print(f"YAML 解析错误: {e}")
    sys.exit(1)
except Exception as e:
    print(f"错误: {e}")
    sys.exit(1)
