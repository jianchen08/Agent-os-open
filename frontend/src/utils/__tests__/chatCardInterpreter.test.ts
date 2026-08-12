/**
 * 功能测试：chat_card 声明解释器（TC S3 翻译层 / chat inline 声明驱动）
 *
 * 推演链：愿景（插件声明能力）→ 前端愿景（工具卡片声明驱动）→ 架构（插件 ui.chat_card
 * 声明 → 翻译成现有 ActivityDetailBlock[] → ActivityCard 原样渲染，不重造块）→ 功能点
 * （"声明 {title 模板, blocks[]} + tool call 上下文 → 解释器产出 ActivityDetailBlock[]"）。
 *
 * 这是 S3 的前端翻译层（模板引擎 + source 求值 + when）。YAML 加载/后端透传（G1）是后端工作。
 * 本测试直接喂声明 + 上下文，断言产出的 ActivityDetailBlock[]——端到端验证翻译逻辑。
 */

import { describe, expect, it } from 'vitest'
import { interpretChatCard } from '@/utils/chatCardInterpreter'
import type { ChatCardDeclaration } from '@/utils/chatCardInterpreter'

describe('功能点：interpretChatCard 把声明翻译成 ActivityDetailBlock[]', () => {
  it('title 模板 + 过滤器（first_line / truncate）求值', () => {
    const decl: ChatCardDeclaration = { title: '{{args.command | first_line | truncate:10}}' }
    const ctx = { args: { command: 'ls -la /very/long/path/here\nsecond line' } }
    const out = interpretChatCard(decl, ctx)
    expect(out.title).toBe('ls -la /ve…')
  })

  it('basename 过滤器取文件名', () => {
    const decl: ChatCardDeclaration = { title: '{{args.path | basename}}' }
    const out = interpretChatCard(decl, { args: { path: '/a/b/c.txt' } })
    expect(out.title).toBe('c.txt')
  })

  it('kv 块：fields 按 source 取值，缺失项跳过', () => {
    const decl: ChatCardDeclaration = {
      blocks: [
        {
          type: 'kv',
          label: '概览',
          fields: [
            { key: '源文', source: 'args.source' },
            { key: '译文', source: 'args.target' },
            { key: '缺失', source: 'args.missing' },
          ],
        },
      ],
    }
    const out = interpretChatCard(decl, { args: { source: 'hello', target: '你好' } })
    expect(out.details).toHaveLength(1)
    expect(out.details[0].contentType).toBe('kv')
    expect(out.details[0].kvItems).toEqual([
      { key: '源文', value: 'hello' },
      { key: '译文', value: '你好' },
    ])
  })

  it('file 块：source 求值为 path', () => {
    const decl: ChatCardDeclaration = {
      blocks: [{ type: 'file', label: '输出文件', source: 'args.out_path' }],
    }
    const out = interpretChatCard(decl, { args: { out_path: '/tmp/result.txt' } })
    expect(out.details[0].contentType).toBe('file')
    expect(out.details[0].path).toBe('/tmp/result.txt')
  })

  it('link 块：source 求值为 url', () => {
    const decl: ChatCardDeclaration = {
      blocks: [{ type: 'link', label: '文档', source: 'args.doc_url' }],
    }
    const out = interpretChatCard(decl, { args: { doc_url: 'https://x.com/y' } })
    expect(out.details[0].contentType).toBe('link')
    expect(out.details[0].url).toBe('https://x.com/y')
  })

  it('when 条件 falsy → 整块不渲染', () => {
    const decl: ChatCardDeclaration = {
      blocks: [
        { type: 'image', source: 'result.screenshot', when: 'result.screenshot' },
        { type: 'log', source: 'result.stdout' },
      ],
    }
    const out = interpretChatCard(decl, { result: { stdout: 'ok', screenshot: null } })
    // image 块 when=result.screenshot 为 null（falsy）→ 跳过；只剩 log
    expect(out.details).toHaveLength(1)
    expect(out.details[0].contentType).toBe('log')
  })

  it('result 的 Python dict 字符串经 safeParse 可被 source 取值', () => {
    const decl: ChatCardDeclaration = {
      blocks: [{ type: 'log', source: 'result.output' }],
    }
    // 后端常把 result 序列化成 "{'output': 'done'}" 形态
    const out = interpretChatCard(decl, { result: "{'output': 'done'}" })
    expect(out.details[0].contentType).toBe('log')
    expect(out.details[0].content).toBe('done')
  })

  it('code 块透传 language + collapsible', () => {
    const decl: ChatCardDeclaration = {
      blocks: [{ type: 'code', label: '命令', source: 'args.cmd', language: 'bash', collapsible: true }],
    }
    const out = interpretChatCard(decl, { args: { cmd: 'echo hi' } })
    expect(out.details[0]).toMatchObject({ contentType: 'code', language: 'bash', collapsible: true, content: 'echo hi' })
  })
})
