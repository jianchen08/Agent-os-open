import json
from collections import Counter

files = Counter()
rules = Counter()
src_errors = 0
non_src_errors = 0
src_rule_breakdown = Counter()
non_src_rule_breakdown = Counter()

with open('scripts/_lint_json.txt', encoding='utf-8') as fh:
    data = json.load(fh)

for entry in data:
    fpath_raw = entry.get('filePath', '')
    fpath = fpath_raw.replace('\\', '/').replace('D:/', '')
    for msg in entry.get('messages', []):
        if msg.get('severity') != 2:  # 2 = error
            continue
        rule = msg.get('ruleId') or '(no-rule)'
        files[fpath] += 1
        rules[rule] += 1
        if '/src/' in fpath or fpath.startswith('src/'):
            src_errors += 1
            src_rule_breakdown[rule] += 1
        else:
            non_src_errors += 1
            non_src_rule_breakdown[rule] += 1

print('TOTAL errors:', sum(rules.values()))
print('SRC errors:', src_errors)
print('NON-SRC (e2e/scripts) errors:', non_src_errors)
print()
print('=== SRC-only error rule breakdown ===')
for r, c in src_rule_breakdown.most_common(30):
    print('%3d  %s' % (c, r))
print()
print('=== NON-SRC error rule breakdown ===')
for r, c in non_src_rule_breakdown.most_common(30):
    print('%3d  %s' % (c, r))
print()
print('=== SRC files with errors (top 30) ===')
for f, c in files.most_common(100):
    if '/src/' in f:
        print('%3d  %s' % (c, f))
