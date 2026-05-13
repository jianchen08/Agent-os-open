import sys
import os
import asyncio
import json
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
os.chdir(os.path.join(os.path.dirname(__file__), '..'))

async def send_msg(proc, msg_dict):
    msg_str = json.dumps(msg_dict, ensure_ascii=False)
    msg_bytes = msg_str.encode('utf-8')
    header = f"Content-Length: {len(msg_bytes)}\r\n\r\n"
    proc.stdin.write(header.encode('utf-8') + msg_bytes)
    await proc.stdin.drain()

async def read_msg(proc):
    headers = {}
    while True:
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=10)
        if not line:
            raise ConnectionError("连接已关闭")
        line = line.decode('utf-8').strip()
        if not line:
            break
        if ':' in line:
            key, value = line.split(':', 1)
            headers[key.strip()] = value.strip()
    cl = int(headers.get('Content-Length', 0))
    if cl == 0:
        return None
    body = await asyncio.wait_for(proc.stdout.read(cl), timeout=10)
    return json.loads(body.decode('utf-8'))

async def main():
    print('1. 启动 pylsp...')
    proc = await asyncio.create_subprocess_exec(
        'pylsp', stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    print(f'   PID: {proc.pid}')

    print('2. 发送 initialize...')
    await send_msg(proc, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"processId": os.getpid(), "rootUri": Path(os.getcwd()).as_uri(), "capabilities": {}},
    })
    resp = await read_msg(proc)
    print(f'   OK: serverInfo={resp.get("result", {}).get("serverInfo", {})}')

    print('3. 发送 initialized...')
    await send_msg(proc, {"jsonrpc": "2.0", "method": "initialized", "params": {}})
    await asyncio.sleep(0.3)

    print('4. 发送 didOpen...')
    test_file = os.path.join(os.getcwd(), 'src', 'tools', 'builtin', 'lsp_tools', 'tool.py')
    uri = Path(test_file).as_uri()
    content = Path(test_file).read_text(encoding='utf-8')
    print(f'   文件: {len(content)} bytes, URI: {uri[:80]}...')

    await send_msg(proc, {
        "jsonrpc": "2.0", "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": uri, "languageId": "python", "version": 1, "text": content}},
    })
    await asyncio.sleep(1)

    try:
        notif = await asyncio.wait_for(read_msg(proc), timeout=3)
        if notif:
            print(f'   通知: method={notif.get("method")}')
            params = notif.get('params', {})
            diags = params.get('diagnostics', [])
            print(f'   诊断数量: {len(diags)}')
            for d in diags[:5]:
                print(f'   - [{d.get("severity")}] L{d.get("range",{}).get("start",{}).get("line")}: {d.get("message")[:80]}')
    except:
        print('   无通知（正常）')

    print('5. 发送 textDocument/definition（查找 Path 导入）...')
    await send_msg(proc, {
        "jsonrpc": "2.0", "id": 2, "method": "textDocument/definition",
        "params": {"textDocument": {"uri": uri}, "position": {"line": 11, "character": 22}},
    })

    for _ in range(5):
        msg = await read_msg(proc)
        if msg and msg.get('id') == 2:
            print(f'   OK: {json.dumps(msg.get("result"), indent=2)[:500]}')
            break
        elif msg:
            print(f'   通知: {msg.get("method")}')

    print('6. 发送 textDocument/references...')
    await send_msg(proc, {
        "jsonrpc": "2.0", "id": 3, "method": "textDocument/references",
        "params": {"textDocument": {"uri": uri}, "position": {"line": 50, "character": 9}, "context": {"includeDeclaration": True}},
    })

    for _ in range(5):
        msg = await read_msg(proc)
        if msg and msg.get('id') == 3:
            print(f'   OK: {json.dumps(msg.get("result"), indent=2)[:500]}')
            break
        elif msg:
            print(f'   通知: {msg.get("method")}')

    proc.terminate()
    await proc.wait()
    print('\nDone')

asyncio.run(main())
