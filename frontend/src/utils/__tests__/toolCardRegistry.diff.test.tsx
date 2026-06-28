/**
 * file_write 工具卡片 diff 渲染验证
 *
 * 验证修复闭环：流式场景下 result 为截断预览字符串、resultData 为完整结构化 dict，
 * enhanceActivityWithToolConfig 仍能正确生成 diffStat（+X -Y 徽标）。
 *
 * 根因背景（修复前）：后端流式 tool_result 事件 result=str(data)[:200] 截断，
 * mergeStreamingParts 保留本地丢弃权威 parts → 前端拿不到 added/removed → diff 不生效。
 * 修复方案：后端补 result_data 结构化字段，前端 toolHandler 存为 resultData，
 * registry 的 extractWriteDiff 优先读 resultData。
 */
import { describe, expect, it } from 'vitest'
import type { ActivityData } from '@/types/activity'
import type { MessageToolCall } from '@/types/models'
import { enhanceActivityWithToolConfig } from '@/utils/toolCardRegistry'

function makeBaseActivity(toolName: string): ActivityData {
  return {
    type: 'tool_call',
    id: 'call_1',
    title: toolName,
    toolName,
    status: 'completed',
    details: [],
    actions: [],
  }
}

describe('file_write 工具卡片 diff（流式 resultData 路径）', () => {
  it('resultData 携带完整 diff 数据 → 生成 diffStat', () => {
    // 流式场景：result 是截断预览字符串，resultData 是完整结构化数据
    const toolCall: MessageToolCall = {
      call_id: 'call_1',
      tool_name: 'file_write',
      tool_args: { action: 'write', path: '/app/x.py', content: 'a\nB\nc\nd' },
      status: 'completed',
      result: "{'file': '/app/x.py', 'lines': 4, 'added': 2, 'rem",  // 截断预览
      resultData: {
        file: '/app/x.py',
        lines: 4,
        added: 2,
        removed: 1,
        old_content: 'a\nb\nc',
        new_content: 'a\nB\nc\nd',
      },
    }

    const enhanced = enhanceActivityWithToolConfig(makeBaseActivity('file_write'), toolCall)

    expect(enhanced.diffStat).toEqual({ added: 2, removed: 1 })
  })

  it('resultData 缺失时回退读 result（历史 API 路径，result 为完整 dict）', () => {
    const toolCall: MessageToolCall = {
      call_id: 'call_2',
      tool_name: 'file_write',
      tool_args: { action: 'search_replace', path: '/app/y.py', old_str: 'x', new_str: 'z' },
      status: 'completed',
      result: { file: '/app/y.py', replacements: 1, added: 1, removed: 1 },
    }

    const enhanced = enhanceActivityWithToolConfig(makeBaseActivity('file_write'), toolCall)

    expect(enhanced.diffStat).toEqual({ added: 1, removed: 1 })
  })

  it('diff 详情块在有 old/new 正文时生成 contentType=diff', () => {
    const toolCall: MessageToolCall = {
      call_id: 'call_3',
      tool_name: 'file_write',
      tool_args: { action: 'write', path: '/app/z.py', content: 'new' },
      status: 'completed',
      resultData: {
        file: '/app/z.py',
        added: 1,
        removed: 0,
        old_content: '',
        new_content: 'new',
      },
    }

    const enhanced = enhanceActivityWithToolConfig(makeBaseActivity('file_write'), toolCall)
    const diffBlock = enhanced.details?.find((d) => d.contentType === 'diff')

    expect(diffBlock).toBeDefined()
    expect(diffBlock?.diffOld).toBe('')
    expect(diffBlock?.diffNew).toBe('new')
    expect(diffBlock?.collapsible).toBe(true)
  })

  it('无 diff 数据（如未完成）→ 不生成 diffStat，不生成 diff 详情块', () => {
    const toolCall: MessageToolCall = {
      call_id: 'call_4',
      tool_name: 'file_write',
      tool_args: { action: 'write', path: '/app/none.py', content: 'x' },
      status: 'running',
      // 流式未完成，无 result / resultData
    }

    const enhanced = enhanceActivityWithToolConfig(makeBaseActivity('file_write'), toolCall)

    expect(enhanced.diffStat).toBeUndefined()
    expect(enhanced.details?.find((d) => d.contentType === 'diff')).toBeUndefined()
  })
})
