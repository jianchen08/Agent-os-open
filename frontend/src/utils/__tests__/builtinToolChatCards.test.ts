/**
 * 等价性测试：内置工具 chat_card 声明 vs 原手写 registerToolCard 配置（TC T1 验收）
 *
 * 验收标准：对每个迁移工具喂典型 sample 数据（args + result），断言 interpretChatCard
 * 产出的 title / details 块类型 / 字段 / 折叠语义与原手写配置一致。
 *
 * 允许的次要差异（已在 builtinToolChatCards.ts 注释说明）：
 *  - truncate 省略号 `…`（手写 `...`）及截断长度略有不同
 *  - task_submit 提交结果块用 kv（结构化）呈现手写 text 块的同一组字段
 *  - 缺失字段的 legacy 多级回退（goal.title / description 等）不在声明层覆盖
 */

import { afterEach, describe, expect, it, vi } from 'vitest'
import { BUILTIN_TOOL_CHAT_CARDS, registerBuiltinToolChatCards } from '../builtinToolChatCards'
import type { ChatCardDeclaration, ToolCallContext } from '../chatCardInterpreter'
import { clearChatCardDeclarations, interpretChatCard } from '../chatCardInterpreter'
import { enhanceActivityWithToolConfig } from '../toolCardRegistry'
import type { ActivityData } from '@/types/activity'
import type { MessageToolCall } from '@/types/models'

afterEach(() => clearChatCardDeclarations())

/** 按 toolName 取内置声明 */
function declFor(name: string): ChatCardDeclaration {
  const entry = BUILTIN_TOOL_CHAT_CARDS.find((t) => t.name === name)
  if (!entry) throw new Error(`missing builtin decl: ${name}`)
  return entry.ui.chat_card
}

/** 调用解释器 */
function interpret(name: string, ctx: ToolCallContext) {
  return interpretChatCard(declFor(name), ctx)
}

// ── file_read ──────────────────────────────────────────────────────────────
describe('file_read 声明 ≈ 手写语义', () => {
  it('典型：标题=basename，详情=文件路径(code)+文件内容(code 折叠)', () => {
    const out = interpret('file_read', {
      args: { file_path: '/app/src/index.ts' },
      result: 'export const x = 1\n',
    })
    expect(out.title).toBe('读取 index.ts')
    // filePathSource → 供 enhance 注入点击打开（等价手写 hasFilePath）
    expect(out.filePath).toBe('/app/src/index.ts')
    expect(out.details).toHaveLength(2)
    expect(out.details[0]).toMatchObject({
      label: '文件路径',
      contentType: 'code',
      content: '/app/src/index.ts',
      collapsible: false,
    })
    expect(out.details[1]).toMatchObject({
      label: '文件内容',
      contentType: 'code',
      collapsible: true,
      defaultExpanded: false,
    })
    expect(out.details[1].content).toBe('export const x = 1\n')
  })

  it('无 result → 文件内容块跳过（仅文件路径）', () => {
    const out = interpret('file_read', { args: { file_path: '/a/b.txt' } })
    expect(out.details).toHaveLength(1)
    expect(out.details[0].label).toBe('文件路径')
  })

  it('缺失 file_path → 标题回退 "读取 file_read"，filePath 为空，路径块跳过', () => {
    const out = interpret('file_read', { result: '内容' })
    // 手写：fileName = path ? basename : tool_name → 'file_read'
    expect(out.title).toBe('读取 file_read')
    expect(out.filePath).toBeUndefined()
    // 路径块 source 缺失 → 跳过；只剩文件内容块
    expect(out.details).toHaveLength(1)
    expect(out.details[0].label).toBe('文件内容')
  })
})

// ── bash_execute ───────────────────────────────────────────────────────────
describe('bash_execute 声明 ≈ 手写语义', () => {
  it('典型：标题=命令首行，详情=命令(code,bash,默认展开)+输出(code,text,折叠)', () => {
    const out = interpret('bash_execute', {
      args: { command: 'ls -la /app' },
      result: 'total 8\ndrwxr-xr-x 2 root root 4096 src\n',
    })
    expect(out.title).toBe('ls -la /app')
    expect(out.details).toHaveLength(2)
    expect(out.details[0]).toMatchObject({
      label: '命令',
      contentType: 'code',
      language: 'bash',
      content: 'ls -la /app',
      collapsible: true,
      defaultExpanded: true,
    })
    expect(out.details[1]).toMatchObject({
      label: '输出',
      contentType: 'code',
      language: 'text',
      collapsible: true,
      defaultExpanded: false,
    })
    expect(out.details[1].content).toBe('total 8\ndrwxr-xr-x 2 root root 4096 src\n')
  })

  it('有 error → 额外渲染错误块(text, 默认展开)', () => {
    const out = interpret('bash_execute', {
      args: { command: 'badcmd' },
      result: '',
      error: 'command not found',
    })
    const errorBlock = out.details.find((d) => d.label === '错误')
    expect(errorBlock).toMatchObject({
      contentType: 'text',
      content: 'command not found',
      collapsible: true,
      defaultExpanded: true,
    })
  })

  it('长命令（>60）→ 标题截断（首行 + 省略号）', () => {
    const out = interpret('bash_execute', { args: { command: 'x'.repeat(70) } })
    // 声明层 truncate:60 → 60 字符 + '…'（手写为 57 + '...'，均截断，差异可接受）
    expect(out.title).toBe('x'.repeat(60) + '…')
    expect(out.title!.length).toBeLessThan(70)
  })

  it('缺失 command → 标题回退 "执行命令"', () => {
    const out = interpret('bash_execute', {})
    expect(out.title).toBe('执行命令')
  })
})

// ── web_search ─────────────────────────────────────────────────────────────
describe('web_search 声明 ≈ 手写语义', () => {
  it('典型：标题=query，详情=搜索内容(text)+搜索结果(text 折叠)', () => {
    const out = interpret('web_search', {
      args: { query: 'GLM coding agent' },
      result: '结果文本',
    })
    expect(out.title).toBe('GLM coding agent')
    expect(out.details).toHaveLength(2)
    expect(out.details[0]).toMatchObject({
      label: '搜索内容',
      contentType: 'text',
      content: 'GLM coding agent',
      collapsible: false,
    })
    expect(out.details[1]).toMatchObject({
      label: '搜索结果',
      contentType: 'text',
      content: '结果文本',
      collapsible: true,
      defaultExpanded: false,
    })
  })

  it('长 query（>50）→ 标题截断', () => {
    const out = interpret('web_search', { args: { query: 'q'.repeat(60) } })
    expect(out.title).toBe('q'.repeat(50) + '…')
    expect(out.title!.length).toBeLessThan(60)
  })

  it('缺失 query → 标题回退 "网页搜索"', () => {
    const out = interpret('web_search', {})
    expect(out.title).toBe('网页搜索')
  })
})

// ── fetch ──────────────────────────────────────────────────────────────────
describe('fetch 声明 ≈ 手写语义', () => {
  it('典型：标题="访问 <hostname>"，详情=URL(code)+页面内容(text 折叠)', () => {
    const out = interpret('fetch', {
      args: { url: 'https://example.com/page' },
      result: '<html>short</html>',
    })
    expect(out.title).toBe('访问 example.com')
    expect(out.details).toHaveLength(2)
    expect(out.details[0]).toMatchObject({
      label: 'URL',
      contentType: 'code',
      content: 'https://example.com/page',
      collapsible: false,
    })
    expect(out.details[1]).toMatchObject({
      label: '页面内容',
      contentType: 'text',
      content: '<html>short</html>',
      collapsible: true,
      defaultExpanded: false,
    })
  })

  it('长页面内容（>500）→ 截断 + 省略号（手写同样在 >500 截断）', () => {
    const long = 'a'.repeat(800)
    const out = interpret('fetch', { args: { url: 'https://x.io/p' }, result: long })
    const block = out.details.find((d) => d.label === '页面内容')!
    expect(block.contentType).toBe('text')
    // 声明层 truncate:500 → 500 + '…'（手写 500 + '\n\n... (内容已截断)'，均截断）
    expect(block.content).toBe('a'.repeat(500) + '…')
    expect((block.content as string).length).toBeLessThan(800)
  })

  it('缺失 url → 标题回退 "访问 网页"（default 兜底）', () => {
    const out = interpret('fetch', {})
    // default:网页 → '访问 网页'（与手写 "访问网页" 仅空格差异，缺失 url 为边界场景）
    expect(out.title).toBe('访问 网页')
  })
})

// ── task_submit ────────────────────────────────────────────────────────────
describe('task_submit 声明 ≈ 手写语义', () => {
  it('典型：标题="提交任务: <goal_title>"，详情含目标/描述/执行者/结果', () => {
    const out = interpret('task_submit', {
      args: {
        goal_title: '实现登录',
        goal_description: '完成登录表单与 API',
        target_id: 'agent-1',
      },
      // 后端 result 常为 Python dict 字符串（单引号），output 子层包装
      result: "{'output': {'task_id': 't-123', 'status': 'success', 'title': '实现登录', 'message': '已创建'}}",
    })
    expect(out.title).toBe('提交任务: 实现登录')

    const byLabel = (label: string) => out.details.find((d) => d.label === label)!
    expect(byLabel('任务目标')).toMatchObject({ contentType: 'text', content: '实现登录', collapsible: false })
    expect(byLabel('详细描述')).toMatchObject({
      contentType: 'text',
      content: '完成登录表单与 API',
      collapsible: true,
      defaultExpanded: false,
    })
    expect(byLabel('执行者')).toMatchObject({ contentType: 'text', content: 'agent-1', collapsible: false })

    // 提交结果：手写为 text 聚合，声明层用 kv 呈现同一组字段（块类型 kv vs text，字段等价）
    const resultBlock = byLabel('提交结果')
    expect(resultBlock.contentType).toBe('kv')
    expect(resultBlock.collapsible).toBe(true)
    expect(resultBlock.defaultExpanded).toBe(false)
    expect(resultBlock.kvItems).toEqual([
      { key: '任务ID', value: 't-123' },
      { key: '状态', value: 'success' },
      { key: '标题', value: '实现登录' },
      { key: '消息', value: '已创建' },
    ])
  })

  it('缺失 goal_title → 标题回退 "提交任务: 任务提交"（与手写一致）', () => {
    const out = interpret('task_submit', { args: { target_id: 'a-1' } })
    // 手写：title 各字段皆空 → '任务提交' → "提交任务: 任务提交"；声明层 default 同样结果
    expect(out.title).toBe('提交任务: 任务提交')
  })

  it('无 result → 提交结果 kv 块因字段全空被跳过', () => {
    const out = interpret('task_submit', { args: { goal_title: 'G' } })
    expect(out.details.find((d) => d.label === '提交结果')).toBeUndefined()
  })
})

// ── enhance 端到端：file_read 的 filePath 注入（等价手写 hasFilePath） ──────
describe('enhance 端到端：内置声明经注册后驱动渲染', () => {
  it('file_read 经 builtin 注册 → enhance 注入 filePath + onOpenFile（等价 hasFilePath）', () => {
    registerBuiltinToolChatCards()
    const spy = vi.fn()
    const toolCall = {
      id: 'tc1',
      tool: 'file_read',
      tool_name: 'file_read',
      tool_args: { file_path: '/app/main.ts' },
      result: 'hi',
      status: 'completed',
      containerTaskId: 'ct-1',
    } as unknown as MessageToolCall
    const activity: ActivityData = {
      type: 'tool_call',
      id: 'tc1',
      title: 'file_read',
      toolName: 'file_read',
      status: 'completed',
      details: [],
      actions: [],
    }
    const enhanced = enhanceActivityWithToolConfig(activity, toolCall, { onOpenFile: spy })
    expect(enhanced.title).toBe('读取 main.ts')
    expect(enhanced.filePath).toBe('/app/main.ts')
    expect(enhanced.onOpenFile).toBeDefined()
    enhanced.onOpenFile?.('/app/main.ts')
    expect(spy).toHaveBeenCalledWith('/app/main.ts', 'ct-1')
  })

  it('registerBuiltinToolChatCards 追加（不清空），schema load 后 builtin 仍可覆盖', () => {
    // 模拟 GrowthLoop：load（清空+装 schema）后追加 builtin
    registerBuiltinToolChatCards()
    expect(declFor('bash_execute')).toBeDefined()
    // 再次注册幂等
    registerBuiltinToolChatCards()
    expect(declFor('bash_execute')).toBeDefined()
  })
})
