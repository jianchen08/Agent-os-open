/** @feature R20 会话标题展示净化 @vision V6 可即用 @ci frontend-test */
/**
 * sanitizeSessionTitle 纯函数测试
 *
 * 断行为：活案例截断链接、完整链接、成对记号、行首记号、混合形态、
 * 纯文本不误伤、幂等与空值边界。不 mock（纯函数无外部依赖）。
 */
import { describe, it, expect } from 'vitest'
import { sanitizeSessionTitle } from '../sessionTitle'

describe('sanitizeSessionTitle：链接', () => {
  it('活案例：被截断的尾部链接保留省略号截断信号', () => {
    expect(sanitizeSessionTitle('[test-upload.txt](/uploads/09f…')).toBe(
      'test-upload.txt…',
    )
  })

  it('完整链接 → 链接文本；图片链接 → alt 文本', () => {
    expect(sanitizeSessionTitle('[文档](https://example.com/a.pdf) 请查收')).toBe(
      '文档 请查收',
    )
    expect(sanitizeSessionTitle('![截图](/uploads/img.png)')).toBe('截图')
  })

  it('被截断但无省略号的尾部链接直接收口；链接后混排普通文本正常', () => {
    expect(sanitizeSessionTitle('参考 [手册](http://x')).toBe('参考 手册')
    expect(sanitizeSessionTitle('[a](b) 说明 [c](d')).toBe('a 说明 c')
  })

  it('疑似但不成链接的方括号不误伤', () => {
    expect(sanitizeSessionTitle('[附录] 与 (备注) 分开阅读')).toBe(
      '[附录] 与 (备注) 分开阅读',
    )
    expect(sanitizeSessionTitle('数组[0] 取值说明')).toBe('数组[0] 取值说明')
  })
})

describe('sanitizeSessionTitle：成对记号', () => {
  it('加粗与斜体剥离，内容保留（两组有区分度输入）', () => {
    expect(sanitizeSessionTitle('**重要提醒**马上处理')).toBe('重要提醒马上处理')
    expect(sanitizeSessionTitle('*要点一* 与 *要点二*')).toBe('要点一 与 要点二')
  })

  it('行内代码反引号剥离', () => {
    expect(sanitizeSessionTitle('运行 `npm run build` 即可')).toBe(
      '运行 npm run build 即可',
    )
  })

  it('下划线记号不剥：snake_case 不误伤', () => {
    expect(sanitizeSessionTitle('foo_bar_baz 字段说明')).toBe(
      'foo_bar_baz 字段说明',
    )
  })

  it('孤立星号（乘法语义）不误伤', () => {
    expect(sanitizeSessionTitle('3*4=12 算式')).toBe('3*4=12 算式')
  })
})

describe('sanitizeSessionTitle：行首记号', () => {
  it('ATX 标题前缀剥离（须后随空白，#1 编号不误伤）', () => {
    expect(sanitizeSessionTitle('## 会议纪要')).toBe('会议纪要')
    expect(sanitizeSessionTitle('### 三级标题内容')).toBe('三级标题内容')
    expect(sanitizeSessionTitle('#1 缺陷追踪')).toBe('#1 缺陷追踪')
  })

  it('残缺强调前缀剥离：首条消息以 ** 开头被截断', () => {
    expect(sanitizeSessionTitle('**加粗开头的长消息被截')).toBe('加粗开头的长消息被截')
    expect(sanitizeSessionTitle('*斜体开头被截')).toBe('斜体开头被截')
  })
})

describe('sanitizeSessionTitle：混合与边界', () => {
  it('多记号叠加：标题前缀 + 加粗包裹链接', () => {
    expect(sanitizeSessionTitle('## **[发布说明](http://x.com/r1)**')).toBe(
      '发布说明',
    )
  })

  it('活案例形态 + 前置强调：先剥链接再剥成对记号', () => {
    expect(sanitizeSessionTitle('**[报告](/f/a.txt)** 与 **结论** 已就绪')).toBe(
      '报告 与 结论 已就绪',
    )
  })

  it('性质断言：幂等（净化输出再净化不变）', () => {
    const cases = [
      '[test-upload.txt](/uploads/09f…',
      '## **[发布说明](http://x.com/r1)**',
      '普通标题without任何记号',
      '',
    ]
    for (const c of cases) {
      const once = sanitizeSessionTitle(c)
      expect(sanitizeSessionTitle(once)).toBe(once)
    }
  })

  it('性质断言：不含 markdown 链接语法残留（链接输入净化后无 [ ]( 形态）', () => {
    const out = sanitizeSessionTitle('[data-file.csv](/uploads/abcdef123.txt')
    expect(out).not.toMatch(/\[[^\]]*\]\(/)
    expect(out.startsWith('data-file.csv')).toBe(true)
  })

  it('空串与纯文本原样返回', () => {
    expect(sanitizeSessionTitle('')).toBe('')
    expect(sanitizeSessionTitle('纯文本会话标题，带逗号。')).toBe(
      '纯文本会话标题，带逗号。',
    )
  })
})
