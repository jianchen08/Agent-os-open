/** @feature FP-RP面板v3.1刀5 内容卡 blocks 渲染 | @ci: frontend-test */
/**
 * content_cards 结构块匹配与 task-list 提取（2026-10-24 用户裁定：
 * "聊天框里的单"渲染归业务插件声明，前端通用消费机制）。
 */
import { describe, expect, it } from 'vitest'
import {
  extractTaskListText,
  matchContentCard,
  matchContentCardForBlock,
  normalizeContentCard,
  type ContentCardDeclaration,
} from '../contentCard'

const mkCard = (over: Partial<ContentCardDeclaration>): ContentCardDeclaration => ({
  id: 'c',
  match: { fences: [] },
  form: 'template',
  pluginId: 'p',
  ...over,
})

describe('normalizeContentCard（结构块声明装载）', () => {
  it('blocks-only 声明（fences 空数组）不再被当坏档丢弃，blocks 原样保留（2026-10-25 端到端实证根修）', () => {
    const card = normalizeContentCard(
      {
        id: 'todo_list',
        match: { fences: [], blocks: ['task-list'] },
        form: 'template',
        template: '<details>{{content}}</details>',
      } as never,
      'pipeline_todo_track',
    )
    expect(card).not.toBeNull()
    expect(card!.match.blocks).toEqual(['task-list'])
    expect(card!.match.fences).toEqual([])
  })

  it('fences 与 blocks 双维在场的声明两维都保留', () => {
    const card = normalizeContentCard(
      {
        id: 'both',
        match: { fences: ['TODO'], blocks: ['task-list'] },
        form: 'css',
      } as never,
      'p',
    )
    expect(card!.match.fences).toEqual(['todo'])
    expect(card!.match.blocks).toEqual(['task-list'])
  })

  it('fences 与 blocks 皆空/缺失仍 fail-closed 丢弃', () => {
    expect(
      normalizeContentCard({ id: 'x', match: { fences: [], blocks: [] }, form: 'css' } as never, 'p'),
    ).toBeNull()
    expect(normalizeContentCard({ id: 'x', form: 'css' } as never, 'p')).toBeNull()
  })
})

describe('matchContentCardForBlock', () => {
  it('命中 match.blocks 含块位的声明；order 小者胜；未命中返回 null', () => {
    const a = mkCard({ id: 'a', match: { fences: [], blocks: ['task-list'] }, order: 50 })
    const b = mkCard({ id: 'b', match: { fences: [], blocks: ['task-list'] }, order: 10 })
    const fenceOnly = mkCard({ id: 'f', match: { fences: ['todo'] } })
    expect(matchContentCardForBlock([fenceOnly, a, b], 'task-list')?.id).toBe('b')
    expect(matchContentCardForBlock([fenceOnly], 'task-list')).toBeNull()
  })
})

describe('extractTaskListText', () => {
  const li = (text: string, checked?: boolean) => ({
    props: {
      children: [
        checked === undefined ? text : { props: { type: 'checkbox', checked } },
        text,
      ],
    },
  })

  it('li 带 checkbox → "- [x]/- [ ] 文本" 行；未带 checkbox 取纯文本', () => {
    const out = extractTaskListText([li('已完成', true), li('待办', false)])
    expect(out).toBe('- [x] 已完成\n- [ ] 待办')
  })

  it('空/畸形输入零产出不炸', () => {
    expect(extractTaskListText(undefined)).toBe('')
    expect(extractTaskListText([null, 42, 'raw'])).toBe('')
  })
})

describe('fence_content_pattern（fence 内容特征维，2026-10-25 裸 HTML 任务卡根修）', () => {
  const raw = (pattern: unknown) => ({
    id: 'todo_list_html',
    match: { fences: ['html', 'todo'], ...(pattern !== undefined ? { fence_content_pattern: pattern } : {}) },
    form: 'webview',
  })
  const cardHtml = '<details style="x">📋 任务清单 3/7</details>'
  const otherHtml = '<p>普通 HTML 文档</p>'

  it('合法 pattern 装载 + 内容特征命中/不命中分流（普通 html 代码块不被劫持）', () => {
    const card = normalizeContentCard(raw('<details[^>]*>.{0,200}任务清单'), 'pipeline_todo_track')
    expect(card?.match.fence_content_pattern).toBe('<details[^>]*>.{0,200}任务清单')
    expect(matchContentCard([card!], 'html', cardHtml)?.id).toBe('todo_list_html')
    expect(matchContentCard([card!], 'html', otherHtml)).toBeNull()
    // 无 pattern 声明对任意同语言内容命中（既有行为零变化）
    const plain = normalizeContentCard(raw(undefined), 'p2')
    expect(matchContentCard([plain!], 'html', otherHtml)?.id).toBe('todo_list_html')
  })

  it('非法正则 / 非字符串 pattern → 整卡拒绝（fail-closed）', () => {
    expect(normalizeContentCard(raw('<details['), 'p')).toBeNull()
    expect(normalizeContentCard(raw(123), 'p')).toBeNull()
  })

  it('pattern 卡在内容缺失时不命中（undefined 内容不参与特征判定）', () => {
    const card = normalizeContentCard(raw('任务清单'), 'p')
    expect(matchContentCard([card!], 'html')).toBeNull()
    expect(matchContentCard([card!], 'html', '含任务清单的围栏')?.id).toBe('todo_list_html')
  })
})

describe('无语言裸围栏的 pattern 卡认领（2026-10-25 实测模型丢语言标注）', () => {
  const raw = {
    id: 'todo_list_html',
    match: { fences: ['html', 'todo'], fence_content_pattern: '<details[^>]*>.{0,200}任务清单' },
    form: 'webview',
  }
  const card = normalizeContentCard(raw, 'pipeline_todo_track')!
  const other = normalizeContentCard(
    { id: 'plain', match: { fences: ['csv'] }, form: 'webview' },
    'p2',
  )

  it('裸围栏 + 内容特征命中 → pattern 卡认领', () => {
    expect(matchContentCard([card, other!], null, '<details>📋 任务清单 2/3</details>')?.id).toBe('todo_list_html')
  })

  it('裸围栏 + 无关内容 → 不命中（无 pattern 卡不参与，防劫持任意裸围栏）', () => {
    expect(matchContentCard([card, other!], null, '随便一段普通代码')).toBeNull()
    expect(matchContentCard([card, other!], null, undefined)).toBeNull()
  })

  it('有语言围栏走原路径不受影响', () => {
    expect(matchContentCard([card, other!], 'todo', '<details>📋 任务清单</details>')?.id).toBe('todo_list_html')
    expect(matchContentCard([card, other!], 'html', '普通 html')).toBeNull()
  })
})
