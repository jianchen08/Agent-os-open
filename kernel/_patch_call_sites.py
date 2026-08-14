"""一次性补丁：server.rs 中所有 process_via_engine 调用点补 thinking_strength 参数（TDD 改动辅助）。"""
import re

p = 'kernel/crates/api/src/server.rs'
src = open(p, encoding='utf-8').read()

# 1) HTTP 调用点 1（单行，8 参数结尾 ""))
assert src.count('&req.history, "", "", "", ""))') == 1
src = src.replace('&req.history, "", "", "", ""))', '&req.history, "", "", "", "", ""))', 1)

# 2) HTTP 调用点 2（&pipeline_id 结尾 &user_id))
assert src.count('&req.history, &pipeline_id, &req.session_id, "", &user_id))') == 1
src = src.replace(
    '&req.history, &pipeline_id, &req.session_id, "", &user_id))',
    '&req.history, &pipeline_id, &req.session_id, "", &user_id, ""))',
    1,
)

# 3) 测试调用点：行内含 process_via_engine( 且以 ("",) 结尾（最后参数是 user_id 占位），
#    补一个 thinking_strength 占位 ""
lines = src.split('\n')
out = []
count = 0
for l in lines:
    stripped = l.rstrip()
    if 'process_via_engine(' in l and re.search(r'"",\s*$', stripped):
        l = re.sub(r'"",\s*$', '"", "",', l)
        count += 1
    out.append(l)
src = '\n'.join(out)
open(p, 'w', encoding='utf-8').write(src)
print(f'patched, test call sites: {count}')
