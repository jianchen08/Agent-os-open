"""检查 config/agents/ 下所有 YAML 文件的必填字段完整性。"""
import yaml
import os

REQUIRED_FIELDS = ['name', 'description', 'version']
TYPE_FIELDS = ['agent_type', 'type']

results = []
for root, dirs, files in os.walk('config/agents'):
    for f in sorted(files):
        if f.endswith(('.yaml', '.yml')):
            path = os.path.join(root, f)
            with open(path, 'r', encoding='utf-8') as fh:
                data = yaml.safe_load(fh)

            issues = []
            for field in REQUIRED_FIELDS:
                if field not in data:
                    issues.append('MISSING: ' + field)

            has_type = any(t in data for t in TYPE_FIELDS)
            if not has_type:
                issues.append('MISSING: agent_type/type')

            name_val = str(data.get('name', 'N/A'))[:40]
            version_val = data.get('version', 'N/A')
            type_val = data.get('agent_type', data.get('type', 'N/A'))

            results.append({
                'path': path,
                'issues': issues,
                'name': name_val,
                'version': version_val,
                'type': type_val,
            })

print(f'Total YAML files: {len(results)}')
print()

ok_count = 0
issue_count = 0
for r in results:
    if r['issues']:
        issue_count += 1
        print(f'[!!] {r["path"]}')
        for iss in r['issues']:
            print(f'     -> {iss}')
        print(f'     name={r["name"]}, ver={r["version"]}, type={r["type"]}')
    else:
        ok_count += 1
        print(f'[OK] {r["path"]}')
        print(f'     name={r["name"]}, ver={r["version"]}, type={r["type"]}')

print()
print(f'Summary: {ok_count} OK, {issue_count} with issues')
