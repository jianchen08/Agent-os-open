/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * mapStateSummaryToViewModel 管道域适配层测试（解耦方案 P2-1）
 *
 * 内核 state 摘要的扁平点号键（task.status / lineage.origin_session_id /
 * track.llm_usage）与 run_status/ended/raw_error 三源状态推断只允许存在于
 * pipelines.ts 适配层——组件层只消费视图模型。本文件锁两件事：
 * 1. 键名映射与语义等值（改一个插件 export_fields 键名只需改 mapper 一处）；
 * 2. 归一运行状态的单一出口（run_status 五态直映，缺省/未知词汇回退
 *    raw_error→failed / ended→completed / running 推断——旧 checkpoint 数据
 *    无 run_status 键时不失真，内核新增状态词汇时前端不崩、走推断兜底）。
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
]

describe('mapStateSummaryToViewModel 归一运行状态（单一状态出口）', () => {
  it('run_status 五态逐一直映（内核 RunStatus 枚举词汇不动语义）', () => {
    for (const status of RUN_STATUS_VOCABULARY) {
      expect(mapStateSummaryToViewModel({ run_status: status }).status).toBe(status)
    }
  })

  it('旧 checkpoint 数据无 run_status：raw_error→failed / ended→completed / 全缺→running', () => {
    expect(mapStateSummaryToViewModel({ raw_error: 'boom' }).status).toBe('failed')
    expect(mapStateSummaryToViewModel({ ended: true }).status).toBe('completed')
    expect(mapStateSummaryToViewModel({}).status).toBe('running')
    // 两者同时在：错误优先于结束（失败态不被 completed 掩盖）
    expect(mapStateSummaryToViewModel({ raw_error: 'boom', ended: true }).status).toBe('failed')
  })

  it('未知状态词汇（内核新增枚举）不崩：回退三源推断（前向兼容守护）', () => {
    expect(mapStateSummaryToViewModel({ run_status: 'evolving' }).status).toBe('running')
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

  it('模式键映射：mode 有值透传，空串/缺失 → undefined（无 mode 键零渲染的取数前提）', () => {
    expect(mapStateSummaryToViewModel({ mode: 'coding' }).mode).toBe('coding')
    expect(mapStateSummaryToViewModel({ mode: 'roleplay' }).mode).toBe('roleplay')
    expect(mapStateSummaryToViewModel({ mode: '' }).mode).toBeUndefined()
    expect(mapStateSummaryToViewModel({}).mode).toBeUndefined()
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

  it('全空摘要：视图模型字段可选不炸（缺省字段 undefined，status 兜底 running）', () => {
    const view = mapStateSummaryToViewModel({})
    expect(view.status).toBe('running')
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
