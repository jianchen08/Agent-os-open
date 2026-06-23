"""Fix task_executor.py path 3: run_in_executor → ensure_future"""
import io

path = r'src\infrastructure\task_executor.py'
with io.open(path, encoding='utf-8') as f:
    lines = f.readlines()

# Find the block to replace
start = None
for i, line in enumerate(lines):
    if '独立线程' in line and 'task_id' in line:
        start = i
        break

if start is None:
    print('NOT FOUND')
    exit(1)

# Find the add_done_callback line
end = None
for i in range(start, min(start + 20, len(lines))):
    if 'add_done_callback(_on_engine_done)' in lines[i]:
        end = i
        break

if end is None:
    print('END NOT FOUND')
    exit(1)

print(f'Replacing lines {start+1}-{end+1}')

new_lines = [
    '                logger.info(\n',
    '                    "TaskWorker: 从历史恢复启动管道（主循环）| task=%s | history_len=%d | pipeline=%s",\n',
    '                    task_id, len(conversation_history), pipeline_id[:12],\n',
    '                )\n',
    '                # REFACTOR: 回主事件循环 — engine.run() 作为协程在主循环运行。\n',
    '                engine_future = asyncio.ensure_future(engine.run(\n',
    '                    user_input="",\n',
    '                    agent_config=agent_config,\n',
    '                    conversation_history=conversation_history,\n',
    '                    task_id=task_id,\n',
    '                    workspace=workspace,\n',
    '                    streaming=True,\n',
    '                    on_chunk=_sink.on_chunk if _sink else lambda chunk: None,\n',
    '                ))\n',
    '                engine_future.add_done_callback(_on_engine_done)\n',
]

lines[start:end+1] = new_lines

with io.open(path, 'w', encoding='utf-8', newline='') as f:
    f.writelines(lines)
print('OK')
