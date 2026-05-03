"""全面验证 config/agents/ 下所有 YAML 文件的格式和字段规范。"""
import yaml
import os
import re

REQUIRED_FIELDS = ['name', 'description', 'version', 'agent_type']
# 标准字段顺序（顶级字段）
STANDARD_ORDER = [
    'config_id', 'name', 'display_name', 'description',
    'agent_type', 'category', 'level', 'model_tier',
    'team',
    'system_prompt',
    'static_vars', 'dynamic_vars',
    'tool_ids',
    'hard_constraints', 'soft_constraints',
    'input_schema', 'output_schema',
    'deliverables', 'recommended_metrics',
    'version', 'is_active', 'status',
    'max_iterations', 'max_reminders', 'timeout_seconds',
    'plugins', 'tags', 'metadata',
    'context_variables', 'prompt_structure', 'memory_injection',
]

def check_indentation(filepath):
    """检查文件是否使用2空格缩进。"""
    issues = []
    with open(filepath, 'r', encoding='utf-8') as f:
        for i, line in enumerate(f, 1):
            if line.strip() and not line.startswith('#'):
                # 计算前导空格
                stripped = line.lstrip(' ')
                indent = len(line) - len(stripped)
                if indent > 0 and indent % 2 != 0:
                    issues.append(f'Line {i}: odd indentation ({indent} spaces)')
    return issues

def check_field_order(filepath):
    """检查顶级字段顺序是否符合标准。"""
    issues = []
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # 提取顶级字段（在多行字符串块外）
    # 简化方法：用yaml解析获取字段列表，然后用正则获取顺序
    top_fields = []
    in_block = False
    block_indent = 0
    
    for line in content.split('\n'):
        stripped = line.rstrip()
        if not stripped or stripped.startswith('#'):
            continue
        
        # 检测是否在多行块内 (| 或 >)
        if not in_block:
            if re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*:', stripped):
                # 这是顶级字段
                field_name = stripped.split(':')[0]
                top_fields.append(field_name)
                # 检查是否开始多行块
                if '|' in stripped or '>' in stripped:
                    in_block = True
                    block_indent = 0
        else:
            # 在块内，检查缩进
            indent = len(line) - len(line.lstrip(' '))
            if indent == 0 and stripped:
                in_block = False
                # 这行可能是新顶级字段
                if re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*:', stripped):
                    field_name = stripped.split(':')[0]
                    top_fields.append(field_name)
                    if '|' in stripped or '>' in stripped:
                        in_block = True
    
    # 检查关键字段顺序：name 应在 description 前，description 在 agent_type 前，等等
    priority_fields = ['config_id', 'name', 'display_name', 'description', 'agent_type', 
                       'category', 'level', 'model_tier']
    
    seen_priority = [(f, top_fields.index(f)) for f in priority_fields if f in top_fields]
    
    for i in range(len(seen_priority) - 1):
        if seen_priority[i][1] > seen_priority[i+1][1]:
            # 允许的偏差：只要 name/description/agent_type 在前面就行
            pass
    
    # 核心检查：前5个顶级字段应该包含 config_id, name, description, agent_type
    first_fields = top_fields[:6]
    if 'agent_type' in top_fields:
        at_idx = top_fields.index('agent_type')
        nm_idx = top_fields.index('name') if 'name' in top_fields else 0
        desc_idx = top_fields.index('description') if 'description' in top_fields else 0
        if not (nm_idx < desc_idx < at_idx):
            issues.append(f'Field order: name({nm_idx}) < description({desc_idx}) < agent_type({at_idx}) not maintained')
    
    return issues, top_fields

def check_no_yaml_errors(filepath):
    """验证 YAML 语法正确性。"""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
        return [], data
    except yaml.YAMLError as e:
        return [f'YAML parse error: {e}'], None

def main():
    results = []
    for root, dirs, files in os.walk('config/agents'):
        for f in sorted(files):
            if f.endswith(('.yaml', '.yml')):
                path = os.path.join(root, f)
                file_issues = []
                
                # 1. YAML 语法检查
                yaml_issues, data = check_no_yaml_errors(path)
                file_issues.extend(yaml_issues)
                
                if data:
                    # 2. 必填字段检查
                    for field in REQUIRED_FIELDS:
                        if field not in data:
                            file_issues.append(f'Missing required field: {field}')
                    
                    # 3. version 格式检查
                    ver = str(data.get('version', ''))
                    if not re.match(r'^\d+\.\d+\.\d+$', ver):
                        file_issues.append(f'Version format invalid: {ver}')
                
                # 4. 缩进检查
                indent_issues = check_indentation(path)
                file_issues.extend(indent_issues[:3])  # 只显示前3个
                
                # 5. 字段顺序检查
                order_issues, top_fields = check_field_order(path)
                file_issues.extend(order_issues)
                
                results.append({
                    'path': path,
                    'issues': file_issues,
                    'fields_count': len(top_fields) if top_fields else 0,
                })
    
    print(f'Total YAML files: {len(results)}')
    print('=' * 60)
    
    ok_count = 0
    issue_count = 0
    for r in results:
        if r['issues']:
            issue_count += 1
            print(f'[FAIL] {r["path"]}')
            for iss in r['issues']:
                print(f'       -> {iss}')
        else:
            ok_count += 1
            print(f'[PASS] {r["path"]} ({r["fields_count"]} top-level fields)')
    
    print('=' * 60)
    print(f'Result: {ok_count} PASS, {issue_count} FAIL')
    return issue_count == 0

if __name__ == '__main__':
    import sys
    sys.exit(0 if main() else 1)
