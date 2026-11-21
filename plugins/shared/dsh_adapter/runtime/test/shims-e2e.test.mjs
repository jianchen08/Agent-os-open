// @feature: FP-0.2.可观测性 dsh_adapter DSH兼容四件垫片 | @vision: V3 可嵌入
// 层 2：四件垫片 × 真实 DSH 包端到端（真实 cordis Context + 真实 npm 插件零改动装载）。
//
// 依赖门控：需要 tmp/probe_hook/node_modules（前序探针实装的
// @deepseek-ai/dsh-repeat-tool-reminder@0.2.1-rc.5 + @deepseek-ai/cordis@4.0.5）。
// 缺失即 skip——真机车道不假绿。
//
// 运行：node --test plugins/shared/dsh_adapter/runtime/test/shims-e2e.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { existsSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

import {
  createIdentityTable,
  dispatchHook,
  makeServiceProxy,
} from '../dsh-compat-shims.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const REPO_ROOT = resolve(here, '../../../../..')
const RUNTIME = join(REPO_ROOT, 'tmp', 'probe_hook')
const REAL_PLUGIN = join(
  RUNTIME, 'node_modules', '@deepseek-ai', 'dsh-repeat-tool-reminder', 'lib', 'index.js',
)
const CORDIS = join(RUNTIME, 'node_modules', '@deepseek-ai', 'cordis', 'lib', 'index.js')

const depsReady = existsSync(REAL_PLUGIN) && existsSync(CORDIS)
const skipOpt = depsReady ? false : 'probe_hook deps missing (true-machine lane: npm install @deepseek-ai/dsh-repeat-tool-reminder@0.2.1-rc.5 @deepseek-ai/cordis@4.0.5)'

async function bootRealPlugin() {
  const { Context } = await import(pathToFileURL(CORDIS).href)
  const mod = await import(pathToFileURL(REAL_PLUGIN).href)
  const ctx = new Context()
  const identities = createIdentityTable()
  const fiber = ctx.plugin(mod, {}) // 真实插件，零改动
  await new Promise((r) => setTimeout(r, 200))
  return { ctx, identities, fiber }
}

test('e2e: real plugin activates (fiber state 2) and registers hooks — zero modification', { skip: skipOpt }, async () => {
  const { fiber } = await bootRealPlugin()
  assert.equal(fiber.state, 2, 'fiber.state 2 = active（真实包零改动激活）')
})

test('e2e: 4 shims × real plugin — loop intervention (additionalContexts at chain threshold)', { skip: skipOpt }, async () => {
  const { ctx, identities } = await bootRealPlugin()

  // this 代理观测探针：用真实 cordis 注册一个三参监听器，记录 this（非箭头函数）。
  let seenThis
  ctx.on('tools/post-execute', function (exec, result, next) {
    seenThis = this
    return next()
  })

  // loop 事件投影：4 帧同参 grep（真实 DSH §1.2 事件表：tools/post-execute(exec, result, next)）
  const emit = async (frameNo) => dispatchHook(ctx, identities, {
    event: 'tools/post-execute',
    exec: { callId: `call-${frameNo}`, name: 'grep', arguments: { q: 'same' } },
    result: { ok: true },
    agentKey: 'sess-1',
  })

  const d1 = await emit(1)
  assert.equal(d1.kind, 'accept', 'frame1：next() 默认决策词表（真实插件链尾放行）')
  await emit(2)
  const d3 = await emit(3)
  const contexts = d3?.additionalContexts ?? []
  assert.ok(
    Array.isArray(contexts) && contexts.length > 0,
    `frame3 必须产出 additionalContexts（身份表使 WeakMap 链计数成立）: ${JSON.stringify(d3)}`,
  )
  const text = JSON.stringify(contexts)
  assert.ok(/repeating/i.test(text), '提醒内容必须是真实插件的 repeat 提醒')

  // this 代理：cordis dispatch 把对象首参当 thisArg（探针 6 实证），垫片三的落地证据
  assert.ok(seenThis !== null && typeof seenThis === 'object', '监听器 this 必须是对象（现有桥形态 this===null）')
  assert.equal(seenThis.agent, identities.agentFor('sess-1'), 'this.agent = 身份表稳定对象')
})

test('e2e: real plugin chain resets on user pre-step (identity table + pre-step projection)', { skip: skipOpt }, async () => {
  const { ctx, identities } = await bootRealPlugin()
  const emitPost = (frameNo) => dispatchHook(ctx, identities, {
    event: 'tools/post-execute',
    exec: { callId: `c-${frameNo}`, name: 'grep', arguments: { q: 'same' } },
    result: { ok: true },
    agentKey: 'sess-1',
  })
  await emitPost(1)
  await emitPost(2)
  const d3 = await emitPost(3)
  assert.ok((d3?.additionalContexts ?? []).length > 0, '前置：链计数到 3 触发提醒')

  // 真实插件 agent/pre-step 监听器：messages 含 user 来源 → chains.delete(agent)（链重置）
  await dispatchHook(ctx, identities, {
    event: 'agent/pre-step',
    payload: { agent: identities.agentFor('sess-1'), messages: [{ source: { kind: 'user' } }] },
  })
  const dAfter = await emitPost(4)
  assert.equal(
    (dAfter?.additionalContexts ?? []).length, 0,
    'pre-step 重置后单帧不得再提醒（计数重新从 1 起）',
  )
})

test('e2e: service proxy injectable as cordis service (agentos.loop.status round trip)', { skip: skipOpt }, async () => {
  const { Context } = await import(pathToFileURL(CORDIS).href)
  const mod = await import(pathToFileURL(REAL_PLUGIN).href)
  const ctx = new Context()

  // 模拟 Python 侧 handler 注册表（真实 stdio 往返在层 3 桥级 E2E / 待真机车道）
  const handlers = new Map([['loop.status', async () => ({ run_status: 'running' })]])
  const rpc = async (method, args) => {
    const h = handlers.get(method)
    if (h === undefined) return { ok: false, error: `no agentos handler: ${method}` }
    return { ok: true, data: await h(...args) }
  }
  ctx.provide('agentos', makeServiceProxy(rpc))

  // 宿主侧自写探针消费插件（复刻探针 5b 手法；不改任何 DSH 安装文件）：
  // 用 DSH 习惯（inject + ctx.<service>）调用 AgentOS 服务面。
  const probeConsumer = {
    name: 'agentos-probe-consumer',
    inject: ['agentos'],
    apply(scope) {
      scope.on('tools/post-execute', async (exec, result, next) => {
        const status = await scope.agentos.loop.status(exec.callId)
        const d = await next()
        return { ...d, probeStatus: status }
      })
    },
  }
  const fiber = ctx.plugin(mod, {})
  const probeFiber = ctx.plugin(probeConsumer, {})
  await new Promise((r) => setTimeout(r, 200))
  assert.equal(fiber.state, 2)
  assert.equal(probeFiber.state, 2, 'inject: [agentos] 的消费插件必须激活（服务面就位）')

  const identities = createIdentityTable()
  const decision = await dispatchHook(ctx, identities, {
    event: 'tools/post-execute',
    exec: { callId: 'call-1', name: 'grep', arguments: {} },
    result: { ok: true },
    agentKey: 'sess-1',
  })
  assert.equal(decision?.probeStatus?.run_status, 'running', '插件内 ctx.agentos.loop.status() 经代理调用成功')

  // fail-visible：未注册 handler → 代理 reject → waterfall 传播（禁止吞异常）
  const failing = {
    name: 'agentos-probe-consumer-missing',
    inject: ['agentos'],
    apply(scope) {
      scope.on('tools/post-execute', async (exec, result, next) => {
        await scope.agentos.loop.stop()
        return next()
      })
    },
  }
  const ctx2 = new Context()
  ctx2.provide('agentos', makeServiceProxy(rpc))
  const fiber2 = ctx2.plugin(failing, {})
  await new Promise((r) => setTimeout(r, 200))
  assert.equal(fiber2.state, 2)
  await assert.rejects(
    () => dispatchHook(ctx2, createIdentityTable(), {
      event: 'tools/post-execute', exec: { callId: 'c2', name: 'x', arguments: {} }, agentKey: 's',
    }),
    /no agentos handler: loop\.stop/,
    '服务缺 handler 必须 fail-visible（错误原样传播）',
  )
})
