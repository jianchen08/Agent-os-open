/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * mapStateSummaryToViewModel 管道域适配层测试（解耦方案 P2-1）
 *
 * 内核 state 摘要的扁平点号键（task.status / lineage.origin_session_id /
 * track.llm_usage）与 run_status/ended/raw_error 三源状态推断只允许存在于
 * pipelines.ts 适配层——组件层只消费视图模型。本文件锁两件事：
 * 1. 键名映射与语义等值（改一个插件 export_fields 键名只需改 mapper 一处）；
 * 2. 归一运行状态的单一出口（run_status 五态直映；ended/raw_error 双源保留
 *    终局推断；三源全缺或词汇未知 = 获取不到运行状态真值 → unknown，不猜
 *    running——用户裁定 2026-11-02「前端状态显示与后端数据完全一致，获取
 *    不到落 unknown」。装机活体 2026-11-01：pending 任务 state 出生行无任何
 *    运行痕迹，旧推断兜底猜 running 显「运行中」，实际从未起跑）。
 *
 * 测试策略：纯函数直测（无网络、无 mock），多组有区分度输入 + 性质断言。
 */

import { describe, it, expect } from 'vitest'
import {
  mapStateSummaryToViewModel,
  mapStateInfoToViewModel,
  type PipelineStateSummary,
} from '../pipelines'
import type { PipelineStatus } from '@/types/pipeline'

const RUN_STATUS_VOCABULARY: PipelineStatus[] = [
  'running',
  'suspended',
  'completed',
  'failed',
  'cancelled',
  'unknown',
]

describe('mapStateSummaryToViewModel 归一运行状态（单一状态出口）', () => {
  it('run_status 五态逐一直映（内核 RunStatus 枚举词汇不动语义）', () => {
    for (const status of RUN_STATUS_VOCABULARY) {
      expect(mapStateSummaryToViewModel({ run_status: status }).status).toBe(status)
    }
  })

  it('三源状态键：raw_error→failed / ended→completed / 全缺→unknown（不猜 running）', () => {
    expect(mapStateSummaryToViewModel({ raw_error: 'boom' }).status).toBe('failed')
    expect(mapStateSummaryToViewModel({ ended: true }).status).toBe('completed')
    // 三源全缺 = 无运行状态真值（如从未起跑的任务管道出生行）→ 诚实落 unknown
    expect(mapStateSummaryToViewModel({}).status).toBe('unknown')
    // 两者同时在：错误优先于结束（失败态不被 completed 掩盖）
    expect(mapStateSummaryToViewModel({ raw_error: 'boom', ended: true }).status).toBe('failed')
  })

  it('未知状态词汇（内核新增枚举）不崩：落 unknown 不猜 running（前向兼容守护）', () => {
    expect(mapStateSummaryToViewModel({ run_status: 'evolving' }).status).toBe('unknown')
    expect(
      mapStateSummaryToViewModel({ run_status: 'evolving', ended: true }).status,
    ).toBe('completed')
    expect(
      mapStateSummaryToViewModel({ run_status: 'evolving', raw_error: 'x' }).status,
    ).toBe('failed')
  })

  it('性质断言：任意输入组合下 status 都落在管道运行状态五态集合内', () => {
    const candidates: PipelineStateSummary[] = [
      {},
      { run_status: 'running' },
      { run_status: 'suspended', ended: true },
      { ended: false, raw_error: null },
      { raw_error: 'x' },
      { run_status: 'mystery', raw_error: 'y', ended: true },
    ]
    for (const c of candidates) {
      expect(RUN_STATUS_VOCABULARY).toContain(mapStateSummaryToViewModel(c).status)
    }
  })
})

describe('mapStateSummaryToViewModel 扁平键 → 视图模型（键名只存在于适配层）', () => {
  it('任务域/血缘键映射：task.status、lineage.origin_session_id、task.ws_meta', () => {
    const view = mapStateSummaryToViewModel({
      'task.status': 'evaluating',
      'lineage.origin_session_id': 'sess-root-1',
      'task.ws_meta': { path: '/ws/task-copy' },
    })
    expect(view.taskStatus).toBe('evaluating')
    expect(view.originSessionId).toBe('sess-root-1')
    expect(view.workspacePath).toBe('/ws/task-copy')
  })

  it('模式键映射：标签集归一透传，空串/缺失 → 空集（无 mode 键零渲染的取数前提）', () => {
    expect(mapStateSummaryToViewModel({ mode: 'coding' }).modeTags).toEqual(['coding'])
    expect(mapStateSummaryToViewModel({ mode: 'roleplay' }).modeTags).toEqual(['roleplay'])
    expect(mapStateSummaryToViewModel({ mode: '' }).modeTags).toEqual([])
    expect(mapStateSummaryToViewModel({}).modeTags).toEqual([])
  })

  it('工作区坐标优先级：task.ws_meta.path > ws_meta.path > workspace（任务管道防会话投影污染）', () => {
    const all = mapStateSummaryToViewModel({
      'task.ws_meta': { path: '/a' },
      ws_meta: { path: '/b' },
      workspace: '/c',
    })
    expect(all.workspacePath).toBe('/a')
    const noTask = mapStateSummaryToViewModel({ ws_meta: { path: '/b' }, workspace: '/c' })
    expect(noTask.workspacePath).toBe('/b')
    const scalar = mapStateSummaryToViewModel({ workspace: '/c' })
    expect(scalar.workspacePath).toBe('/c')
  })

  it('空串/空对象坐标与状态：不伪造非空值（空串映射为 undefined）', () => {
    const view = mapStateSummaryToViewModel({
      'task.status': '',
      workspace: '',
      ws_meta: {},
    })
    expect(view.taskStatus).toBeUndefined()
    expect(view.workspacePath).toBeUndefined()
  })

  it('LLM 观测键映射：llm_model / context_window / track.llm_usage 原值透传', () => {
    const usage = { total_tokens: 42, last_input_tokens: 10, last_output_tokens: 4 }
    const view = mapStateSummaryToViewModel({
      llm_model: 'MiniMax-M3',
      context_window: 204800,
      'track.llm_usage': usage,
    })
    expect(view.llmModel).toBe('MiniMax-M3')
    expect(view.contextWindow).toBe(204800)
    expect(view.llmUsage).toEqual(usage)
  })

  it('压缩终态映射：context.compression 只收 applied/failed（不发明状态）', () => {
    const failed = mapStateSummaryToViewModel({
      'context.compression': {
        status: 'failed',
        reason: 'no healthy deployments',
        ended_at: '2026-10-28T07:50:48Z',
      },
    })
    expect(failed.compressionStatus).toEqual({
      status: 'failed',
      reason: 'no healthy deployments',
    })

    const applied = mapStateSummaryToViewModel({
      'context.compression': { status: 'applied' },
    })
    expect(applied.compressionStatus).toEqual({ status: 'applied', reason: undefined })

    // 未知/缺失状态一律 undefined（不展示假状态）
    expect(
      mapStateSummaryToViewModel({ 'context.compression': { status: 'weird' } })
        .compressionStatus,
    ).toBeUndefined()
    expect(mapStateSummaryToViewModel({}).compressionStatus).toBeUndefined()
  })

  it('phase/message/end/error 展示字段映射；非真 ended 不置位', () => {
    const view = mapStateSummaryToViewModel({
      current_phase: 'main',
      message_count: 7,
      ended: false,
      raw_error: null,
    })
    expect(view.currentPhase).toBe('main')
    expect(view.messageCount).toBe(7)
    expect(view.ended).toBeUndefined()
    expect(view.rawError).toBeUndefined()
    const ended = mapStateSummaryToViewModel({ ended: true, raw_error: 'late failure' })
    expect(ended.ended).toBe(true)
    expect(ended.rawError).toBe('late failure')
  })

  it('错误兜底：raw_error 缺席时回退 run.plugin_errors 全量拼接（ADR 2026-10-27）', () => {
    // 折叠失败轮真值形态：raw_error 恒缺席 + run_stop_reason 恒空串 + 镜像键在场
    const view = mapStateSummaryToViewModel({
      run_status: 'failed',
      raw_error: null,
      run_stop_reason: '',
      'run.plugin_errors': [
        { plugin_id: 'pipeline_llm_core', code: 'MCP_CALL_FAILED', message: '' },
        {
          plugin_id: 'pipeline_llm_core',
          code: 'MCP_CALL_FAILED',
          message: 'RateLimitError - [1302][您的账户已达到速率限制]',
        },
        {
          plugin_id: 'pipeline_other',
          code: 'PLUGIN_EXEC_FAILED',
          message: '第二个错误原文',
        },
      ],
    })
    // 空文本条目跳过；多条逐行拼接全量透传不截断
    expect(view.rawError).toBe(
      'RateLimitError - [1302][您的账户已达到速率限制]\n第二个错误原文',
    )
    // raw_error 优先（引擎投影键在场时不做拼接回退）
    const precedence = mapStateSummaryToViewModel({
      raw_error: 'engine projected',
      'run.plugin_errors': [{ message: 'mirror' }],
    })
    expect(precedence.rawError).toBe('engine projected')
    // 镜像键全空/形态漂移 → undefined（不伪造空串语义）
    expect(
      mapStateSummaryToViewModel({ 'run.plugin_errors': [{ message: '  ' }] }).rawError,
    ).toBeUndefined()
    expect(mapStateSummaryToViewModel({}).rawError).toBeUndefined()
  })

  it('终局署名映射：非空透传，空串/缺失不渲染（run_stop_reason 基线出口 2026-10-25）', () => {
    expect(
      mapStateSummaryToViewModel({ run_stop_reason: 'empty_reply_exhausted' }).stopReason,
    ).toBe('empty_reply_exhausted')
    // 空串 = 未署名（每轮终态都写防跨 run 残留）→ 不伪造为有署名
    expect(mapStateSummaryToViewModel({ run_stop_reason: '' }).stopReason).toBeUndefined()
    expect(mapStateSummaryToViewModel({}).stopReason).toBeUndefined()
  })

  it('挂起归因映射（P-23 片2）：suspended_reason 非空透传，空串/缺失缺省（两向）', () => {
    expect(
      mapStateSummaryToViewModel({
        suspended_reason: 'memory watermark self-heal: private bytes 持续超阈值',
      }).suspendedReason,
    ).toBe('memory watermark self-heal: private bytes 持续超阈值')
    // 空串 = 未署名（drain 写面防跨 run 残留语义）→ 缺省不伪造为有归因
    expect(mapStateSummaryToViewModel({ suspended_reason: '' }).suspendedReason).toBeUndefined()
    expect(mapStateSummaryToViewModel({}).suspendedReason).toBeUndefined()
  })

  it('任务失败归因映射（P-21）：task.failure_* 非空透传，空串/缺失回退现状不伪造', () => {
    const view = mapStateSummaryToViewModel({
      'task.failure_class': 'run_stopped',
      'task.failure_reason': '运行终止（LLM 连续同类失败循环）',
    })
    expect(view.failureClass).toBe('run_stopped')
    expect(view.failureReason).toBe('运行终止（LLM 连续同类失败循环）')
    // 空串/缺失 = 无归因（字段缺失回退现状：归因行不渲染，不伪造空值语义）
    expect(mapStateSummaryToViewModel({ 'task.failure_class': '' }).failureClass).toBeUndefined()
    expect(mapStateSummaryToViewModel({}).failureReason).toBeUndefined()
    // 单键在场：只透传在场的键（分类与全文互不牵连）
    const partial = mapStateSummaryToViewModel({ 'task.failure_class': 'run_aborted' })
    expect(partial.failureClass).toBe('run_aborted')
    expect(partial.failureReason).toBeUndefined()
  })

  it('全空摘要：视图模型字段可选不炸（缺省字段 undefined，status 诚实落 unknown）', () => {
    const view = mapStateSummaryToViewModel({})
    expect(view.status).toBe('unknown')
    expect(view.currentPhase).toBeUndefined()
    expect(view.llmUsage).toBeUndefined()
  })
})

describe('mapStateInfoToViewModel 条目级映射', () => {
  it('携带条目级坐标（pipeline_id/thread_id）并内嵌摘要视图', () => {
    const entry = mapStateInfoToViewModel({
      pipeline_id: 'p-1',
      thread_id: 't-1',
      source: 'memory',
      state: { run_status: 'suspended', current_phase: 'main' },
    })
    expect(entry.pipelineId).toBe('p-1')
    expect(entry.threadId).toBe('t-1')
    expect(entry.status).toBe('suspended')
    expect(entry.currentPhase).toBe('main')
  })

  it('thread_id 缺省（孤儿管道）不伪造', () => {
    const entry = mapStateInfoToViewModel({
      pipeline_id: 'p-2',
      source: 'checkpoint',
      state: {},
    })
    expect(entry.threadId).toBeUndefined()
  })
})
