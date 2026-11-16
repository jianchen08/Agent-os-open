/** @feature FP-T12 前端组件补测 | @ci: frontend-test */
/**
 * contentCard 残余覆盖补测：
 * - 声明里带非法正则的围栏内容特征：判定 fail-closed 返回不命中（不静默放行）
 * - task-list 文本提取的复选框发现递归：无复选框的 children 数组、深层嵌套
 *   children、无 props 的对象节点三条路径都按「未勾选/无产出」处理
 *
 * 都是纯函数行为，无 mock（无外部依赖）。
 */

import { describe, expect, it } from 'vitest'
import {
  extractTaskListText,
  matchContentCard,
  type ContentCardDeclaration,
} from '../contentCard'

function makeCard(overrides: Partial<ContentCardDeclaration> = {}): ContentCardDeclaration {
  return {
    id: 'card-a',
    match: { fences: ['todo'] },
    form: 'css',
    pluginId: 'host',
    ...overrides,
  }
}

describe('matchContentCard — fence_content_pattern 非法正则', () => {
  it('非法正则整条不命中（fail-closed，不把异常抛给渲染层）', () => {
    const broken = makeCard({
      match: { fences: ['todo'], fence_content_pattern: '[unclosed' },
    })

    expect(matchContentCard([broken], 'todo', '任意内容')).toBeNull()
  })

  it('合法正则命中时照常认领（同一输入形态、不同声明可区分）', () => {
    const ok = makeCard({
      match: { fences: ['todo'], fence_content_pattern: '^清单' },
    })

    expect(matchContentCard([ok], 'todo', '清单：两项')?.id).toBe('card-a')
    expect(matchContentCard([ok], 'todo', '别的内容')).toBeNull()
  })
})

describe('extractTaskListText — 复选框发现递归', () => {
  it('children 数组内无复选框 → 按未勾选行输出', () => {
    const li = { props: { children: ['买牛奶'] } }

    expect(extractTaskListText([li])).toBe('- [ ] 买牛奶')
  })

  it('复选框嵌套在子元素 children 内 → 逐层发现勾选态', () => {
    const nested = {
      props: {
        children: [
          {
            props: {
              children: [{ props: { type: 'checkbox', checked: true } }, '写周报'],
            },
          },
        ],
      },
    }

    expect(extractTaskListText([nested])).toBe('- [x] 写周报')
  })

  it('无 props 的对象子节点 → 视为无复选框、零文本产出', () => {
    const li = { props: { children: [{}] } }

    expect(extractTaskListText([li])).toBe('')
  })
})
