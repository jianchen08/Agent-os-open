/**
 * when 条件表达式求值器测试（ADR §3.4）
 *
 * ADR §3.4 when 子句：让贡献点动态显隐。需 context keys 基础集 +
 * 基本运算符（&&/||/==/!）。本测试只验求值逻辑，不涉及 React 渲染。
 *
 * context keys 基础集：
 * - pipeline.running / pipeline.idle
 * - workspace.focus / chat.focus
 * - resource.isFile / resource.extname
 * - interaction.pending
 */

import { describe, it, expect } from 'vitest'
import { evaluateWhen, type ContextKeys } from '@/services/schema/whenExpression'

describe('evaluateWhen — 空表达式', () => {
  it('when 为 undefined 时恒可见', () => {
    expect(evaluateWhen(undefined, {})).toBe(true)
  })

  it('when 为空字符串时恒可见', () => {
    expect(evaluateWhen('', {})).toBe(true)
  })

  it('when 为纯空白时恒可见', () => {
    expect(evaluateWhen('   ', {})).toBe(true)
  })
})

describe('evaluateWhen — 单 context key（truthy 判定）', () => {
  const ctx: ContextKeys = {
    'pipeline.running': true,
    'pipeline.idle': false,
    'workspace.focus': true,
    'chat.focus': false,
    'resource.isFile': true,
    'resource.extname': '.py',
    'interaction.pending': false,
  }

  it('布尔 true 的 key 命中', () => {
    expect(evaluateWhen('pipeline.running', ctx)).toBe(true)
  })

  it('布尔 false 的 key 失配', () => {
    expect(evaluateWhen('pipeline.idle', ctx)).toBe(false)
  })

  it('未声明的 key 视为 false', () => {
    expect(evaluateWhen('nonexistent.key', ctx)).toBe(false)
  })

  it('字符串值非空时 truthy', () => {
    expect(evaluateWhen('resource.extname', ctx)).toBe(true)
  })

  it('字符串值空时 falsy', () => {
    expect(evaluateWhen('resource.extname', { ...ctx, 'resource.extname': '' })).toBe(false)
  })
})

describe('evaluateWhen — == 相等运算符', () => {
  const ctx: ContextKeys = {
    'pipeline.running': true,
    'resource.extname': '.py',
    'workspace.focus': true,
  }

  it('字符串相等命中', () => {
    expect(evaluateWhen("resource.extname == '.py'", ctx)).toBe(true)
  })

  it('字符串相等失配', () => {
    expect(evaluateWhen("resource.extname == '.js'", ctx)).toBe(false)
  })

  it('布尔相等命中（true == true）', () => {
    expect(evaluateWhen('pipeline.running == true', ctx)).toBe(true)
  })

  it('布尔相等失配（false == true）', () => {
    expect(evaluateWhen('pipeline.idle == true', ctx)).toBe(false)
  })

  it('双引号字符串字面量也支持', () => {
    expect(evaluateWhen('resource.extname == ".py"', ctx)).toBe(true)
  })

  it('== 两侧允许任意空格', () => {
    expect(evaluateWhen("resource.extname   ==   '.py'", ctx)).toBe(true)
  })
})

describe('evaluateWhen — != 不等运算符', () => {
  const ctx: ContextKeys = { 'resource.extname': '.py', 'pipeline.running': true }

  it('不等命中', () => {
    expect(evaluateWhen("resource.extname != '.js'", ctx)).toBe(true)
  })

  it('不等失配（实际相等）', () => {
    expect(evaluateWhen("resource.extname != '.py'", ctx)).toBe(false)
  })
})

describe('evaluateWhen — ! 逻辑非', () => {
  const ctx: ContextKeys = { 'pipeline.running': true, 'pipeline.idle': false }

  it('对 true key 取反得 false', () => {
    expect(evaluateWhen('!pipeline.running', ctx)).toBe(false)
  })

  it('对 false key 取反得 true', () => {
    expect(evaluateWhen('!pipeline.idle', ctx)).toBe(true)
  })

  it('对未声明 key 取反得 true', () => {
    expect(evaluateWhen('!nonexistent.key', ctx)).toBe(true)
  })

  it('对相等表达式取反', () => {
    expect(evaluateWhen("!(resource.extname == '.py')", { 'resource.extname': '.js' })).toBe(true)
  })
})

describe('evaluateWhen — && 逻辑与', () => {
  const ctx: ContextKeys = {
    'pipeline.running': true,
    'workspace.focus': true,
    'chat.focus': false,
  }

  it('两真命中', () => {
    expect(evaluateWhen('pipeline.running && workspace.focus', ctx)).toBe(true)
  })

  it('一真一假失配', () => {
    expect(evaluateWhen('pipeline.running && chat.focus', ctx)).toBe(false)
  })

  it('两侧皆假失配', () => {
    expect(evaluateWhen('chat.focus && nonexistent', ctx)).toBe(false)
  })
})

describe('evaluateWhen — || 逻辑或', () => {
  const ctx: ContextKeys = {
    'pipeline.running': true,
    'chat.focus': false,
  }

  it('一真一假命中', () => {
    expect(evaluateWhen('pipeline.running || chat.focus', ctx)).toBe(true)
  })

  it('两侧皆假失配', () => {
    expect(evaluateWhen('chat.focus || nonexistent', ctx)).toBe(false)
  })
})

describe('evaluateWhen — 运算符优先级与组合', () => {
  const ctx: ContextKeys = {
    'pipeline.running': true,
    'workspace.focus': true,
    'chat.focus': false,
    'resource.isFile': true,
  }

  it('! 优先于 &&', () => {
    // !chat.focus && pipeline.running = true && true = true
    expect(evaluateWhen('!chat.focus && pipeline.running', ctx)).toBe(true)
  })

  it('&& 优先于 ||', () => {
    // chat.focus && pipeline.running || workspace.focus
    // = (false && true) || true = false || true = true
    expect(evaluateWhen('chat.focus && pipeline.running || workspace.focus', ctx)).toBe(true)
  })

  it('括号强制分组', () => {
    // chat.focus || (pipeline.running && workspace.focus) = false || true = true
    expect(evaluateWhen('chat.focus || (pipeline.running && workspace.focus)', ctx)).toBe(true)
  })

  it('括号内为假整体为假', () => {
    // pipeline.running && (chat.focus || nonexistent) = true && false = false
    expect(evaluateWhen('pipeline.running && (chat.focus || nonexistent)', ctx)).toBe(false)
  })

  it('== 与 && 组合', () => {
    expect(evaluateWhen('pipeline.running && resource.isFile == true', ctx)).toBe(true)
  })

  it('复杂组合', () => {
    // (!chat.focus && pipeline.running) || resource.extname == '.py'
    const ctx2: ContextKeys = { ...ctx, 'resource.extname': '.py' }
    expect(evaluateWhen('!chat.focus && pipeline.running || resource.extname == \'.py\'', ctx2)).toBe(true)
  })
})

describe('evaluateWhen — 真实场景（ADR §3.4 列举）', () => {
  it('pipeline.running 场景：运行中可见', () => {
    expect(evaluateWhen('pipeline.running', { 'pipeline.running': true })).toBe(true)
  })

  it('workspace.focus 场景：失焦时隐藏', () => {
    expect(evaluateWhen('workspace.focus', { 'workspace.focus': false })).toBe(false)
  })

  it('resource.isFile && resource.extname == \'.py\' 仅 Python 文件可见', () => {
    const pyCtx: ContextKeys = { 'resource.isFile': true, 'resource.extname': '.py' }
    const jsCtx: ContextKeys = { 'resource.isFile': true, 'resource.extname': '.js' }
    expect(evaluateWhen("resource.isFile && resource.extname == '.py'", pyCtx)).toBe(true)
    expect(evaluateWhen("resource.isFile && resource.extname == '.py'", jsCtx)).toBe(false)
  })

  it('interaction.pending 场景：无待处理时隐藏', () => {
    expect(evaluateWhen('interaction.pending', { 'interaction.pending': false })).toBe(false)
  })
})

describe('evaluateWhen — 健壮性', () => {
  it('非法表达式不抛异常，返回 false', () => {
    expect(evaluateWhen('&&&&', {})).toBe(false)
  })

  it('未闭合括号返回 false', () => {
    expect(evaluateWhen('(pipeline.running', { 'pipeline.running': true })).toBe(false)
  })

  it('未知运算符返回 false', () => {
    expect(evaluateWhen('pipeline.running > 1', { 'pipeline.running': true })).toBe(false)
  })
})
