// @feature FP-T12 前端适配 | @vision V6 可即用 | @ci frontend-test
/**
 * resolveAgentIcon 单测（agent 图标化：config/agents/**\/*.yaml 的 `icon` 字段）。
 *
 * 契约（用户定的机制，优先级从高到低）：
 * 1. emoji（`icon: "🧠"`）——原样作为文本渲染，零资源文件；
 * 2. 内置图标名（`icon: "brain"`）——解析到前端图标集里的具名组件；
 * 3. 未配置 / 空串 / 空白 / 未知名字 → 缺省图标兜底，不留空。
 *
 * 防拟合：每种分支≥2 组有区分度输入（不同 emoji、不同内置名、不同空值形态），
 * 并含性质断言（同一输入恒等、未知名恒落缺省）。
 */
import { describe, expect, it } from 'vitest'
import { Bot, Brain, Search, Target } from '@/assets/icons'
import {
  AGENT_BUILTIN_ICONS,
  DEFAULT_AGENT_ICON,
  resolveAgentIcon,
  resolveAgentIconValue,
} from '../agentIcon'

describe('resolveAgentIcon — emoji 优先', () => {
  it('配置 emoji → 原样返回 emoji（两组不同 emoji 均成立）', () => {
    expect(resolveAgentIcon('🧠')).toEqual({ kind: 'emoji', value: '🧠' })
    expect(resolveAgentIcon('🔍')).toEqual({ kind: 'emoji', value: '🔍' })
  })

  it('emoji 前后空白被裁剪（不是整串照搬）', () => {
    expect(resolveAgentIcon('  🧠  ')).toEqual({ kind: 'emoji', value: '🧠' })
  })

  it('emoji 优先于内置名：emoji 不与内置名混判', () => {
    // 同一组件对两个输入给出不同 kind，防止实现一律返回缺省也能绿
    expect(resolveAgentIcon('brain').kind).toBe('builtin')
    expect(resolveAgentIcon('🧠').kind).toBe('emoji')
  })
})

describe('resolveAgentIcon — 内置图标名', () => {
  it('配置内置名 → 解析到图标集具名组件（两组不同名指向不同组件）', () => {
    const brain = resolveAgentIcon('brain')
    expect(brain.kind).toBe('builtin')
    expect(brain.kind === 'builtin' && brain.Icon).toBe(Brain)

    const search = resolveAgentIcon('search')
    expect(search.kind).toBe('builtin')
    expect(search.kind === 'builtin' && search.Icon).toBe(Search)
    // 性质：两个具名内置名解析出的组件互不相同
    expect(brain.kind === 'builtin' && search.kind === 'builtin' && brain.Icon !== search.Icon).toBe(true)
  })

  it('内置名大小写不敏感 + 容忍空白', () => {
    const upper = resolveAgentIcon('BRAIN')
    expect(upper.kind === 'builtin' && upper.Icon).toBe(Brain)
    const padded = resolveAgentIcon('  target ')
    expect(padded.kind === 'builtin' && padded.Icon).toBe(Target)
  })
})

describe('resolveAgentIcon — 缺省兜底', () => {
  it('未配置 / 空串 / 纯空白 → 缺省图标（三组空值形态均落缺省，不留空）', () => {
    for (const raw of [undefined, null, '', '   ']) {
      const spec = resolveAgentIcon(raw as string | undefined | null)
      expect(spec.kind).toBe('default')
      expect(spec.kind === 'default' && spec.Icon).toBe(DEFAULT_AGENT_ICON)
    }
  })

  it('未知内置名 → 缺省（不抛错、不留空）', () => {
    const spec = resolveAgentIcon('no_such_icon_zzz')
    expect(spec.kind).toBe('default')
    expect(spec.kind === 'default' && spec.Icon).toBe(Bot)
  })

  it('缺省图标就是 Bot（单一真值常量，不在调用方各写一个）', () => {
    expect(DEFAULT_AGENT_ICON).toBe(Bot)
  })
})

describe('resolveAgentIcon — 稳定性与映射表', () => {
  it('同一输入多次调用结果恒等（纯函数，无内部状态）', () => {
    expect(resolveAgentIcon('brain')).toEqual(resolveAgentIcon('brain'))
    expect(resolveAgentIcon('🔍')).toEqual(resolveAgentIcon('🔍'))
  })

  it('映射表是单一真值：表内每个名字都能解析出 builtin 且不影响缺省判定', () => {
    const names = Object.keys(AGENT_BUILTIN_ICONS)
    expect(names.length).toBeGreaterThanOrEqual(6)
    for (const name of names) {
      const spec = resolveAgentIcon(name)
      expect(spec.kind).toBe('builtin')
      expect(spec.kind === 'builtin' && spec.Icon).toBe(AGENT_BUILTIN_ICONS[name])
    }
  })
})

describe('resolveAgentIconValue — 从 agents 注册表取原始 icon 配置', () => {
  const AGENTS = [
    { id: 'agentos', configId: 'agentos', name: '灵汐', icon: '🧠' },
    { id: 'general_agent', configId: 'general_agent', name: '通用任务执行者', icon: 'wrench' },
    { id: 'review_agent', configId: 'review_agent', name: '复盘分析专家' },
  ]

  it('注册表命中且配了 icon → 原值（id 与 configId 双键同形均命中）', () => {
    expect(resolveAgentIconValue('agentos', AGENTS)).toBe('🧠')
    expect(resolveAgentIconValue('general_agent', AGENTS)).toBe('wrench')
  })

  it('注册表命中但未配 icon → undefined（交给缺省兜底，不伪造值）', () => {
    expect(resolveAgentIconValue('review_agent', AGENTS)).toBeUndefined()
  })

  it('注册表未命中 / 空 id → undefined（不猜、不借用其它 agent 的 icon）', () => {
    expect(resolveAgentIconValue('mode_coding/unknown_agent', AGENTS)).toBeUndefined()
    expect(resolveAgentIconValue(undefined, AGENTS)).toBeUndefined()
    expect(resolveAgentIconValue('', AGENTS)).toBeUndefined()
  })

  it('与名称解析同源：同一 agentId 在名字与图标两处命中同一条注册表行', () => {
    // 性质断言：两人同源，命中与否必须一致
    for (const id of ['agentos', 'review_agent', 'nope']) {
      const name = AGENTS.find((a) => a.id === id || a.configId === id)
      const icon = resolveAgentIconValue(id, AGENTS)
      expect(icon).toBe(name?.icon)
    }
  })
})
