/** @feature FP-0.2.二 前端持久化内存修复 | @ci: frontend-test */
/**
 * handleToolResult 释放 partialOutput 测试
 *
 * 背景（2026-11-12 内存修复）：handleToolProgress 为执行中工具累计 stdout
 * 缓冲 partialOutput（单调用上限 64KB）。旧实现 tool_result 到达时只写
 * result/resultData/error/durationMs，partialOutput 永不清理——长任务数百次
 * 工具调用线性滞留数十 MB（内存 + 持久化双重放大）。
 *
 * 契约：tool_result 落地即释放 partialOutput（执行中 stdout 的权威视图是
 * result/resultData，执行输出实时流不再需要）。
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { handleToolResult } from '../toolHandler'

const PID = 'pipe-result-release'
const MID = 'msg-result-release'

function setupToolPart(partialOutput?: string[]) {
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
            {
              type: 'tool_call',
              callId: 'call_r1',
              name: 'bash_execute',
              args: {},
              state: 'calling',
              ...(partialOutput ? { partialOutput } : {}),
            },
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

function resultEvent(extra: Record<string, unknown> = {}) {
  return {
    type: 'tool_result',
    data: {
      pipeline_id: PID,
      message_id: MID,
      call_id: 'call_r1',
      tool_name: 'bash_execute',
      success: true,
      result: 'done output',
      duration_ms: 1200,
      ...extra,
    },
  }
}

describe('handleToolResult partialOutput 释放', () => {
  beforeEach(() => {
    usePipelineMessageStore.setState({
      messagesByPipeline: {},
      streamingState: {},
      activePipelineId: PID,
    })
  })

  it('tool_result 落地即释放 partialOutput（不再滞留执行中 stdout 缓冲）', () => {
    setupToolPart(['chunk1\n', 'chunk2\n'])
    handleToolResult(resultEvent())

    const part = getPart()
    expect(part.state).toBe('done')
    expect(part.result).toBe('done output')
    expect(part.partialOutput).toBeUndefined()
  })

  it('成功与失败结果都释放缓冲（失败路径同样不滞留）', () => {
    setupToolPart(['partial stdout'])
    handleToolResult(resultEvent({ success: false, error: 'boom' }))

    const part = getPart()
    expect(part.state).toBe('error')
    expect(part.partialOutput).toBeUndefined()
  })

  it('原本无 partialOutput 的工具调用不受影响', () => {
    setupToolPart()
    handleToolResult(resultEvent())

    const part = getPart()
    expect(part.state).toBe('done')
    expect(part.partialOutput).toBeUndefined()
  })
})
