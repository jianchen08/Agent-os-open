/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * WS 事件名单单源契约测试（ADR 2026-10-10-ws-events-single-source）
 *
 * 单源 = config/kernel/ws_events.yaml：前端 WS_SERVER_EVENTS 由该文件派生
 * （Vite ?raw + js-yaml），内核 stream 路由族与域事件镜像名单 include_str!
 * 同读该文件。本测试解析磁盘上的同一物理文件与前端消费面对账（真单源：
 * 各自解析同一文件，集合相等；替代已退役的源码文本扫描对账）。
 *
 * 锁四件事：
 * 1. 键集合/值逐对相等：派生常量与 yaml server_events 完全一致
 *    （词汇表镜像漂移、派生实现失真均红）；
 * 2. 名单卫生：幽灵事件词汇不得回流；
 * 3. 内核消费节完整性：stream_family / domain_mirrors 结构合法（缺节即红，
 *    内核侧 include_str 解析由 Rust 测试同文件锁定）；
 * 4. K3 四方契约锁定：approval.created 事件名在插件发射面（server.py）、
 *    清单面（approval/plugin.json ui_schema trigger = 前端订阅声明面）、
 *    单源面（yaml domain_mirrors = 内核路由面）三方同文名——任一方改名即红
 *    （内核路由行为面由 kernel cargo 测试同 commit 锁定：字面量行为测试 +
 *    yaml 跟随测试）。
 */

import { readFileSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { describe, it, expect } from 'vitest'
import { parseYamlStrict, serializeYaml } from '@/services/schema/yaml'
import { WS_SERVER_EVENTS, deriveWsServerEventsFromSource } from '@/constants/websocket'

const REPO_ROOT = resolve(__dirname, '../../../..')
const WS_EVENTS_PATH = join(REPO_ROOT, 'config/kernel/ws_events.yaml')

/** 单源文件 ws_events 节的结构形状（server_events 键值 + 内核消费两节） */
interface WsEventsDocShape {
  stream_family: unknown
  domain_mirrors: unknown
  server_events: Record<string, unknown>
}

/** 解析磁盘上的单源文件（与生产 ?raw 内嵌同源同库） */
function loadWsEventsDoc(): { ws_events: WsEventsDocShape } {
  const text = readFileSync(WS_EVENTS_PATH, 'utf-8')
  const doc = parseYamlStrict(text) as Record<string, unknown>
  const wsEvents = doc?.ws_events
  if (!wsEvents || typeof wsEvents !== 'object') {
    throw new Error('ws_events.yaml 缺 ws_events 节')
  }
  return { ws_events: wsEvents as WsEventsDocShape }
}

/** 已知幽灵词汇：后端（kernel 事件族 + 插件 event-bus.emit）均无发射源 */
const KNOWN_GHOST_EVENTS = [
  'state_change',
  'schema_updated',
  'execution_start',
  'execution_progress',
  'execution_done',
  'execution_cancelled',
  'execution_output',
  'sub_agent_created',
  'sub_agent_waiting_input',
  'sub_agent_completed',
  'agent_level_changed',
  'session_update',
  'pipeline_received',
  'approval_request',
  'node_status_update',
  'execution_status_update',
  'execution_event',
  'workflow_step_update',
  'execution_control_response',
  'agent_inject_response',
]

describe('WS 事件名单单源对账（ADR 2026-10-10）', () => {
  it('派生常量与磁盘单源文件键集合/值逐对相等（真单源机械闸）', () => {
    const source = loadWsEventsDoc().ws_events.server_events
    const derivedKeys = Object.keys(WS_SERVER_EVENTS).sort()
    const sourceKeys = Object.keys(source).sort()
    expect(derivedKeys).toEqual(sourceKeys)

    const mismatched: string[] = []
    for (const key of sourceKeys) {
      if (WS_SERVER_EVENTS[key as keyof typeof WS_SERVER_EVENTS] !== source[key]) {
        mismatched.push(key)
      }
    }
    expect(mismatched).toEqual([])
    // 性质断言：名单非空且事件名无重复（值是 wire 契约，重名=路由歧义）
    const names = Object.values(source)
    expect(names.length).toBeGreaterThan(0)
    expect(new Set(names).size).toBe(names.length)
  })

  it('订阅型事件名钉死字面量（裸字符串订阅收口不回退）', () => {
    // 有区分度的字面量钉子：yaml 改错这两条 wire 名（或派生失真）即红
    expect(WS_SERVER_EVENTS.PENDING_INPUTS_CHANGED).toBe('pending_inputs_changed')
    expect(WS_SERVER_EVENTS.COMPRESSION_FAILED).toBe('compression_failed')
    expect(WS_SERVER_EVENTS.APPROVAL_TAKEN).toBe('approval.taken')
  })

  it('幽灵事件零残留：已知无发射源词汇不得回流名单', () => {
    const values = Object.values(WS_SERVER_EVENTS)
    for (const ghost of KNOWN_GHOST_EVENTS) {
      expect(values).not.toContain(ghost)
    }
  })

  it('内核消费节结构合法（stream_family / domain_mirrors：非空字符串数组、无重复）', () => {
    const { stream_family: family, domain_mirrors: mirrors } = loadWsEventsDoc().ws_events
    for (const [label, section] of [
      ['stream_family', family],
      ['domain_mirrors', mirrors],
    ] as const) {
      expect(Array.isArray(section), `${label} 须为数组`).toBe(true)
      const list = section as unknown[]
      expect(list.length, `${label} 不得为空`).toBeGreaterThan(0)
      for (const name of list) {
        expect(typeof name, `${label} 条目须为字符串`).toBe('string')
        expect(name as string).not.toBe('')
      }
      expect(new Set(list).size, `${label} 不得重复`).toBe(list.length)
    }
  })
})

describe('approval.created 四方契约锁定（K3）', () => {
  const APPROVAL_CREATED = 'approval.created'

  /** 单源面：yaml domain_mirrors 收录该事件（= 内核路由面，include_str 同读） */
  function domainMirrorsFromSource(): string[] {
    const mirrors = loadWsEventsDoc().ws_events.domain_mirrors
    expect(Array.isArray(mirrors), 'domain_mirrors 须为数组').toBe(true)
    return mirrors as string[]
  }

  it('单源面：domain_mirrors 收录 approval.created', () => {
    expect(domainMirrorsFromSource()).toContain(APPROVAL_CREATED)
  })

  it('插件发射面：approval server.py 以同名发射 event-bus', () => {
    const py = readFileSync(join(REPO_ROOT, 'plugins/shared/approval/server.py'), 'utf-8')
    // 定点单文件断言（非全仓扫描）：emit 载荷 "event": "<name>" 字面量存在
    const pattern = new RegExp(
      `["']event["']\\s*:\\s*["']${APPROVAL_CREATED.replace('.', '\\.')}["']`,
    )
    expect(pattern.test(py), 'server.py 须以同名字面量发射 approval.created').toBe(true)
  })

  it('清单面（= 前端订阅声明面）：approval plugin.json 全屏 widget 以同名 on_event 触发', () => {
    const manifest = JSON.parse(
      readFileSync(join(REPO_ROOT, 'plugins/shared/approval/plugin.json'), 'utf-8'),
    ) as {
      ui_schema?: { widgets?: Array<{ trigger?: string; space?: string }> }
    }
    const widgets = manifest.ui_schema?.widgets ?? []
    const hit = widgets.some(
      (w) => w.trigger === `on_event:${APPROVAL_CREATED}` && w.space === 'fullscreen',
    )
    // 前端 SchemaFullscreenHost 从该 trigger 声明动态订阅（parseEventTrigger），
    // 清单即订阅面——trigger 改名则前端订阅静默失联，此处即红
    expect(hit, 'plugin.json 须声明 on_event:approval.created 的全屏 widget').toBe(true)
  })
})

describe('deriveWsServerEventsFromSource fail-closed 行为', () => {
  // 真实单源文本 = 好路径基准；漂移用例在其上做最小变异后重序列化
  const realSource = readFileSync(WS_EVENTS_PATH, 'utf-8')

  /** 深拷贝解析产物并重序列化为变异 yaml 文本 */
  function mutate(rebuild: (doc: Record<string, unknown>) => void): string {
    const doc = JSON.parse(JSON.stringify(parseYamlStrict(realSource))) as Record<string, unknown>
    rebuild(doc)
    return serializeYaml(doc)
  }

  it('真实单源文本派生结果与导出常量逐对相等（好路径）', () => {
    const derived = deriveWsServerEventsFromSource(realSource)
    expect({ ...derived }).toEqual({ ...WS_SERVER_EVENTS })
    expect(Object.isFrozen(derived)).toBe(true)
  })

  it('缺 ws_events.server_events 节 → 抛错', () => {
    expect(() => deriveWsServerEventsFromSource('schema_version: 1')).toThrow(
      /缺 ws_events\.server_events/,
    )
    expect(() => deriveWsServerEventsFromSource('ws_events:\n  stream_family: [a]')).toThrow(
      /缺 ws_events\.server_events/,
    )
  })

  it('键集合漂移双向抛错：缺键与多键分别报缺失/多余', () => {
    expect(() =>
      deriveWsServerEventsFromSource(
        mutate((doc) => {
          delete (doc.ws_events as Record<string, unknown>).server_events.HEARTBEAT
        }),
      ),
    ).toThrow(/missing=\[HEARTBEAT\]/)
    expect(() =>
      deriveWsServerEventsFromSource(
        mutate((doc) => {
          ;(
            (doc.ws_events as Record<string, unknown>).server_events as Record<string, unknown>
          ).GHOST = 'ghost_event'
        }),
      ),
    ).toThrow(/extra=\[GHOST\]/)
  })

  it('值契约违规抛错：非字符串与空串', () => {
    expect(() =>
      deriveWsServerEventsFromSource(
        mutate((doc) => {
          ;(
            (doc.ws_events as Record<string, unknown>).server_events as Record<string, unknown>
          ).ITERATION = 3
        }),
      ),
    ).toThrow(/ITERATION.*非空字符串/)
    expect(() =>
      deriveWsServerEventsFromSource(
        mutate((doc) => {
          ;(
            (doc.ws_events as Record<string, unknown>).server_events as Record<string, unknown>
          ).ITERATION = ''
        }),
      ),
    ).toThrow(/ITERATION.*非空字符串/)
  })

  it('坏 yaml 语法与空文档向上传播（不回落空名单）', () => {
    expect(() => deriveWsServerEventsFromSource('ws_events: [unclosed')).toThrow()
    expect(() => deriveWsServerEventsFromSource('')).toThrow()
  })
})
