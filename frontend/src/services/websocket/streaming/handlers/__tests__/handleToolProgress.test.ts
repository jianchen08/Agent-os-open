/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * handleToolProgress 测试（task_observability 任务 2）
 *
 * bash 等长任务执行中经 frontend.emit 推 tool_progress（stdout 增量）：
 * - call_id 定位 tool_call part → 追加 partialOutput（尾部截断防膨胀）
 * - currentStep 更新为「已输出 X KB / Ys」运行时摘要
 * - part 处于 calling 状态时保持不变（进度 ≠ 完成）
 * - 缺 call_id / 找不到 part → 静默跳过（tool_start 未达等场景）
 * - part 已 done → 忽略迟到进度（结果已定，避免覆盖）
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { handleToolProgress } from '../toolHandler'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'

const PID = 'pipe-progress'
const MID = 'msg-progress'

function setupToolPart(state: 'calling' | 'done' = 'calling') {
  usePipelineMessageStore.setState({
    messagesByPipeline: {
      [PID]: [
        {
          id: MID,
          sessionId: 's1',
          sequence: 1,
          role: 'assistant',
          content: '',
          timestamp: '',
          status: 'streaming',
          parts: [
            { type: 'tool_call', callId: 'call_p1', name: 'bash_execute', args: {}, state },
          ],
        } as any,
      ],
    },
  })
}

function getPart() {
  const msgs = usePipelineMessageStore.getState().getMessages(PID)
  return msgs[0]?.parts?.[0] as any
}

function progressEvent(delta: string, extra: Record<string, unknown> = {}) {
  return {
    type: 'tool_progress',
    data: {
      pipeline_id: PID,
      message_id: MID,
      call_id: 'call_p1',
      tool_name: 'bash_execute',
      delta,
      bytes_read: 1024,
      elapsed_ms: 2100,
      ...extra,
    },
  }
}

describe('handleToolProgress', () => {
  beforeEach(() => {
    usePipelineMessageStore.setState({
      messagesByPipeline: {},
      streamingState: {},
      activePipelineId: PID,
    })
  })

  it('追加 delta 到 partialOutput 并更新 currentStep', () => {
    setupToolPart()
    handleToolProgress(progressEvent('build step 1 ok\n'))
    handleToolProgress(progressEvent('build step 2 ok\n'))

    const part = getPart()
    expect(part.partialOutput).toHaveLength(2)
    expect(part.partialOutput[0]).toBe('build step 1 ok\n')
    expect(part.currentStep).toContain('1.0 KB')
    expect(part.currentStep).toContain('2.1s')
    // 进度不改变 calling 状态
    expect(part.state).toBe('calling')
  })

  it('partialOutput 尾部截断（防超大输出膨胀）', () => {
    setupToolPart()
    // 连续推 40 次 4KB delta（远超 64KB 上限）
    for (let i = 0; i < 40; i++) {
      handleToolProgress(progressEvent('x'.repeat(4096)))
    }
    const part = getPart()
    const totalChars = part.partialOutput.join('').length
    expect(totalChars).toBeLessThanOrEqual(64 * 1024 + 4096) // 上限 + 单条余量
    // 保留的是尾部（最新输出）
    expect(part.partialOutput[part.partialOutput.length - 1]).toContain('x')
  })

  it('缺 call_id 时静默跳过', () => {
    setupToolPart()
    const event = progressEvent('no call')
    delete event.data.call_id
    handleToolProgress(event)
    expect(getPart().partialOutput ?? []).toHaveLength(0)
  })

  it('part 不存在时静默跳过（tool_start 未达）', () => {
    handleToolProgress(progressEvent('orphan'))
    // 不崩溃即通过
  })

  it('part 已 done 时忽略迟到进度', () => {
    setupToolPart('done')
    handleToolProgress(progressEvent('late delta'))
    expect(getPart().partialOutput ?? []).toHaveLength(0)
  })
})
