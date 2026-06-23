"""Delete orphaned finally-block lines from task_executor.py"""
import io, sys

path = 'src/infrastructure/task_executor.py'
with io.open(path, 'r', encoding='utf-8') as f:
    content = f.read()

# Find and remove the orphaned code block
# Line pattern: after REFACTOR comment, before "class TaskExecutorMixin:"
old_block_start = content.find('#   客户端绑定在已死循环上，下次引擎复用时触发')
if old_block_start == -1:
    print('NOT FOUND')
    sys.exit(1)

old_block_end = content.find('class TaskExecutorMixin:', old_block_start)
if old_block_end == -1:
    print('CLASS NOT FOUND')
    sys.exit(1)

# Remove the block
content = content[:old_block_start] + content[old_block_end:]

with io.open(path, 'w', encoding='utf-8', newline='') as f:
    f.write(content)

# Verify
import ast
ast.parse(content)
print('FIXED OK')
