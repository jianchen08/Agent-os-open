/**
 * ChatContainer equality 函数测试
 *
 * 验证 Zustand selector 的 equality 函数在各种场景下是否正确判断数据变化
 * 这直接决定 React 是否重渲染消息列表
 */

describe('ChatContainer pipelineMessages equality 函数', () => {
  let EMPTY_MESSAGES: any[]

  const equalityFn = (a: any[], b: any[]): boolean => {
    if (a === b) return true
    if (!Array.isArray(a) || !Array.isArray(b)) return false
    if (a.length !== b.length) return false
    if (a.length === 0 && b.length === 0) return true
    for (let i = 0; i < a.length; i++) {
      if (a[i] !== b[i]) return false
    }
    return true
  }

  beforeEach(() => {
    EMPTY_MESSAGES = []
  })

  it('同引用返回 true（无变化）', () => {
    const arr = [{ id: '1' }]
    expect(equalityFn(arr, arr)).toBe(true)
  })

  it('两个空数组返回 true', () => {
    expect(equalityFn([], [])).toBe(true)
  })

  it('长度不同返回 false（有新消息！）', () => {
    const old = [{ id: 'msg-1' }]
    const cur = [{ id: 'msg-1' }, { id: 'msg-2' }]
    expect(equalityFn(old, cur)).toBe(false)
  })

  it('长度相同、内容相同引用返回 true', () => {
    const msg1 = { id: 'msg-1' }
    const msg2 = { id: 'msg-2' }
    const old = [msg1, msg2]
    const cur = [msg1, msg2]
    expect(equalityFn(old, cur)).toBe(true)
  })

  it('长度相同、某项引用变化返回 false（消息被 updateMessage 更新）', () => {
    const msg1 = { id: 'msg-1' }
    const msg2 = { id: 'msg-2', content: 'old' }
    const old = [msg1, msg2]
    const cur = [msg1, { ...msg2, content: 'new' }]
    expect(equalityFn(old, cur)).toBe(false)
  })

  it('EMPTY_MESSAGES 常量与新空数组返回 true', () => {
    expect(equalityFn(EMPTY_MESSAGES, [])).toBe(true)
  })

  it('从空数组到1条消息返回 false（用户发了第一条消息）', () => {
    const old = EMPTY_MESSAGES
    const cur = [{ id: 'msg-1', role: 'user', content: 'hello' }]
    expect(equalityFn(old, cur)).toBe(false)
  })

  it('从1条到2条返回 false（AI 回复了）', () => {
    const userMsg = { id: 'msg-1', role: 'user', content: 'hello' }
    const old = [userMsg]
    const cur = [userMsg, { id: 'msg-2', role: 'assistant', content: '' }]
    expect(equalityFn(old, cur)).toBe(false)
  })

  it('从2条到3条返回 false（streaming 占位符添加）', () => {
    const msgs = [
      { id: 'msg-1', role: 'user' },
      { id: 'msg-2', role: 'assistant', content: 'prev' },
    ]
    const cur = [...msgs, { id: 'msg-3', role: 'assistant', status: 'streaming' }]
    expect(equalityFn(msgs, cur)).toBe(false)
  })

  it('3条到3条但中间一条被替换返回 false', () => {
    const m1 = { id: '1' }
    const m2 = { id: '2', status: 'streaming' }
    const m3 = { id: '3' }
    const old = [m1, m2, m3]
    const cur = [m1, { ...m2, status: 'completed' }, m3]
    expect(equalityFn(old, cur)).toBe(false)
  })

  describe('BUG 回归: a.length !== b.length 时 return true 的错误', () => {
    it('旧版 return true 会导致新消息不渲染', () => {
      const buggyFn = (a: any[], b: any[]): boolean => {
        if (a === b) return true
        if (!Array.isArray(a) || !Array.isArray(b)) return false
        if (a.length !== b.length) return true  // ← BUG!
        if (a.length === 0 && b.length === 0) return true
        for (let i = 0; i < a.length; i++) {
          if (a[i] !== b[i]) return false
        }
        return true
      }

      const old = [{ id: 'msg-1' }]
      const cur = [{ id: 'msg-1' }, { id: 'msg-2' }]

      // buggy 版本认为"相等"，React 不重渲染
      expect(buggyFn(old, cur)).toBe(true)
      // 修复后版本认为"不等"，React 重渲染
      expect(equalityFn(old, cur)).toBe(false)
    })
  })
})
