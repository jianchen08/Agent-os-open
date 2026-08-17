# -*- coding: utf-8 -*-
"""清理历史测试会话 + 孤儿任务管道（一次性脚本，针对根目录真实库）。

真实数据源：agentos_kernel.db（内核实际使用；kernel/agentos_kernel.db 为遗留库）。

删除目标（2026-08-16 e2e/任务工具测试产物，共 8 会话 + 孤儿管道 + 无管道老 run）：
- thread-85cd8203（提交一个简单的任务…）/ fe0abe14 / 32d0b304 / c3eb6b4a
- thread-bc990305 / 5615f823 / 70154bf5 / 1a29fdf8
- 孤儿管道 60de945b（thread_id = 管道自身 ID，无会话归属）
- 3 条 pipeline_id 为 NULL 的老 run（bec3afc4 / 0e3b11fa / bd4d8b08）

保留：thread-d8ce13db（用户当前新会话，00:05 创建）及其管道 dabf1368 与 2 条 run。
修复：保留会话 metadata 缺 session_type → 补 'main_pipeline'（否则会话列表接口过滤后看不到）。
"""
import sqlite3
import sys

DB = 'agentos_kernel.db'

KEEP_SESSION = 'thread-d8ce13db-43ce-462d-8200-98ab946bd064'
KEEP_PIPELINE = 'dabf1368cbd14ec0b845ec850d9fb76c'


def main() -> int:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # 从 DB 反查实际存在的会话（防止写死 thread_id 拼错导致漏删）
    rows = cur.execute(
        "SELECT thread_id, title FROM sessions WHERE thread_id != ? ORDER BY created_at",
        (KEEP_SESSION,),
    ).fetchall()
    if not rows:
        print('[INFO] 没有可删除的会话（已干净？）')
    else:
        print('=== 将删除的会话 ===')
        for r in rows:
            print(f'  {r["thread_id"][:32]} | {r["title"][:50]}')
    if cur.execute('SELECT COUNT(*) FROM sessions WHERE thread_id = ?', (KEEP_SESSION,)).fetchone()[0] == 0:
        print(f'[ERROR] 保留会话不存在: {KEEP_SESSION}，中止')
        return 1

    # 收集这些会话的管道
    threads = [r['thread_id'] for r in rows]
    pipelines = []
    if threads:
        th_ph = ','.join('?' * len(threads))
        for r in cur.execute(
            f'SELECT pipeline_id FROM pipeline_sessions WHERE thread_id IN ({th_ph})', threads
        ).fetchall():
            pipelines.append(r['pipeline_id'])
    # 孤儿管道（thread_id = 自身，且不在保留管道中）
    orphan = cur.execute(
        'SELECT pipeline_id FROM pipeline_sessions WHERE pipeline_id = thread_id AND pipeline_id != ?',
        (KEEP_PIPELINE,),
    ).fetchall()
    for r in orphan:
        if r['pipeline_id'] not in pipelines:
            pipelines.append(r['pipeline_id'])
    # 会话表 pipeline_ids 兜底补充
    if threads:
        th_ph = ','.join('?' * len(threads))
        for r in cur.execute(f'SELECT pipeline_ids FROM sessions WHERE thread_id IN ({th_ph})', threads).fetchall():
            if r['pipeline_ids']:
                for pid in __import__('json').loads(r['pipeline_ids']):
                    if pid not in pipelines:
                        pipelines.append(pid)

    print(f'\n=== 将删除的管道 ({len(pipelines)}) ===')
    for p in pipelines:
        print(f'  {p}')
    print(f'保留管道: {KEEP_PIPELINE}')

    # run_id 集合：按管道收集 + 无管道归属的老 run（排除保留管道 run）
    pid_ph = ','.join('?' * len(pipelines)) if pipelines else "''"
    run_ids = []
    if pipelines:
        for r in cur.execute(
            f'SELECT DISTINCT run_id FROM message_slots WHERE pipeline_id IN ({pid_ph})', pipelines
        ).fetchall():
            if r['run_id'] and r['run_id'] not in run_ids:
                run_ids.append(r['run_id'])
        for r in cur.execute(
            f'SELECT run_id FROM runs WHERE pipeline_id IN ({pid_ph})', pipelines
        ).fetchall():
            if r['run_id'] and r['run_id'] not in run_ids:
                run_ids.append(r['run_id'])
    # 无管道归属的老 run（pipeline_id IS NULL 且不属于保留管道）
    for r in cur.execute(
        'SELECT run_id FROM runs WHERE pipeline_id IS NULL OR pipeline_id = ""'
    ).fetchall():
        if r['run_id'] and r['run_id'] not in run_ids:
            run_ids.append(r['run_id'])

    print(f'\n=== 将删除的 run ({len(run_ids)}) ===')
    for r in run_ids:
        print(f'  {r[:12]}...')

    # ── 预检各表行数 ──
    print('\n=== 删除范围预检 ===')
    for t in ['message_slots', 'pipeline_checkpoints', 'pipeline_state', 'pipeline_sessions']:
        if pipelines:
            n = cur.execute(f'SELECT COUNT(*) FROM {t} WHERE pipeline_id IN ({pid_ph})', pipelines).fetchone()[0]
        else:
            n = 0
        print(f'{t} (pipeline): {n}')
    if pipelines:
        n = cur.execute('SELECT COUNT(*) FROM execution_records WHERE pipeline_run_id IN (%s)' % pid_ph,
                        pipelines).fetchone()[0]
        print(f'execution_records (pipeline_run_id): {n}')
    if threads:
        th_ph = ','.join('?' * len(threads))
        n = cur.execute(f'SELECT COUNT(*) FROM sessions WHERE thread_id IN ({th_ph})', threads).fetchone()[0]
        print(f'sessions (thread): {n}')
    if run_ids:
        rid_ph = ','.join('?' * len(run_ids))
        for t in ['runs', 'traces', 'branches', 'pipeline_run_summaries']:
            n = cur.execute(f'SELECT COUNT(*) FROM {t} WHERE run_id IN ({rid_ph})', run_ids).fetchone()[0]
            print(f'{t} (run): {n}')
    else:
        print('runs/traces/branches/summaries: 无 run（跳过）')

    answer = input('\n确认清理以上数据？[y/N] ')
    if answer.strip().lower() != 'y':
        print('已取消')
        return 1

    # ── 执行（单事务）──
    try:
        cur.execute('BEGIN')
        if pipelines:
            for t in ['message_slots', 'pipeline_checkpoints', 'pipeline_state', 'pipeline_sessions']:
                cur.execute(f'DELETE FROM {t} WHERE pipeline_id IN ({pid_ph})', pipelines)
            cur.execute('DELETE FROM execution_records WHERE pipeline_run_id IN (%s)' % pid_ph, pipelines)
        if run_ids:
            rid_ph = ','.join('?' * len(run_ids))
            for t in ['traces', 'branches', 'pipeline_run_summaries', 'runs']:
                cur.execute(f'DELETE FROM {t} WHERE run_id IN ({rid_ph})', run_ids)
        if threads:
            th_ph = ','.join('?' * len(threads))
            cur.execute('DELETE FROM pipeline_sessions WHERE thread_id IN (%s)' % th_ph, threads)
            cur.execute('DELETE FROM sessions WHERE thread_id IN (%s)' % th_ph, threads)
        # 修复保留会话 metadata：补 session_type（缺失才插入）
        cur.execute(
            "UPDATE sessions SET metadata = json_set(COALESCE(metadata, '{}'), '$.session_type', 'main_pipeline') "
            "WHERE thread_id = ? AND json_extract(COALESCE(metadata, '{}'), '$.session_type') IS NULL",
            (KEEP_SESSION,),
        )
        cur.execute('COMMIT')
    except Exception as e:
        cur.execute('ROLLBACK')
        print(f'[ERROR] 清理失败，已回滚: {e}')
        return 1

    # ── 结果校验 ──
    print('\n=== 清理结果 ===')
    print(f'sessions 剩余: {cur.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]}')
    print(f'runs 剩余: {cur.execute("SELECT COUNT(*) FROM runs").fetchone()[0]}')
    print(f'message_slots 剩余: {cur.execute("SELECT COUNT(*) FROM message_slots").fetchone()[0]}')
    print(f'pipeline_sessions 剩余: {cur.execute("SELECT COUNT(*) FROM pipeline_sessions").fetchone()[0]}')
    print(f'pipeline_checkpoints 剩余: {cur.execute("SELECT COUNT(*) FROM pipeline_checkpoints").fetchone()[0]}')
    print(f'pipeline_state 剩余: {cur.execute("SELECT COUNT(*) FROM pipeline_state").fetchone()[0]}')
    print('\n保留的会话:')
    for r in cur.execute('SELECT thread_id, title, metadata FROM sessions').fetchall():
        print(f'  {r["title"]} | {r["thread_id"][:20]}... | {r["metadata"]}')
    print('\n保留的管道:')
    for r in cur.execute('SELECT pipeline_id, thread_id FROM pipeline_sessions').fetchall():
        print(f'  {r["pipeline_id"][:20]}... -> {r["thread_id"][:20]}...')

    conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
