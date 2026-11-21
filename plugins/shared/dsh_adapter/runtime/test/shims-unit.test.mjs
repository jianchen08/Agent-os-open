// @feature: FP-0.2.可观测性 dsh_adapter DSH兼容四件垫片
// 层 1：四件垫片纯逻辑单测（node:test，零外部依赖，node --test 手动车道未挂 CI）。
// 契约来源：docs/working/dad914f83c1e_DSH兼容层可行性验证.md §2.7 条件集
// （身份表 / 三参签名 / this 代理 / 服务代理）。
// 运行：node --test plugins/shared/dsh_adapter/runtime/test/shims-unit.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  createIdentityTable,
  hookArgsFor,
  defaultDecisionFor,
  makeThisArg,
  makeServiceProxy,
  dispatchHook,
} from '../dsh-compat-shims.mjs'

// ── 垫片一：身份表 ─────────────────────────────────────────────

test('identity: same agent key returns the same object (WeakMap key semantics)', () => {
  const t = createIdentityTable()
  const a1 = t.agentFor('sess-1')
  const a2 = t.agentFor('sess-1')
  assert.equal(a1, a2, '同 key 必须返回同一对象引用')
  assert.notEqual(a1, t.agentFor('sess-2'), '不同 key 必须不同对象')
})

test('identity: identity table holds distinct keys apart (>=2 groups)', () => {
  const t = createIdentityTable()
  const seen = new Set()
  for (const key of ['a', 'b', 'c', 'a']) {
    seen.add(t.agentFor(key))
  }
  assert.equal(seen.size, 3, '4 次取用 3 个 key → 恰好 3 个稳定对象')
})

test('identity: exec objects are stable per callId and built lazily once', () => {
  const t = createIdentityTable()
  let builds = 0
  const factory = () => {
    builds += 1
    return { callId: 'c1', name: 'grep' }
  }
  const e1 = t.execFor('c1', factory)
  const e2 = t.execFor('c1', factory)
  assert.equal(e1, e2)
  assert.equal(builds, 1, '同 callId 二次取用不得重建（WeakMap 键语义）')
  assert.notEqual(t.execFor('c2', factory), e1)
  assert.equal(builds, 2)
})

test('identity: non-string agent key fails loud (no silent null key)', () => {
  const t = createIdentityTable()
  assert.throws(() => t.agentFor(undefined), TypeError)
  assert.throws(() => t.agentFor(null), TypeError)
  assert.throws(() => t.agentFor(42), TypeError)
})

// ── 垫片二：三参签名（参数展开 + 默认决策词表） ─────────────────

test('hookArgs: tools/post-execute expands to (exec, result, next)', () => {
  const exec = { name: 'grep', agent: { id: 's1' } }
  const result = { ok: true }
  const shape = hookArgsFor('tools/post-execute', { exec, result })
  assert.equal(shape.mode, 'waterfall')
  assert.equal(shape.injectsNext, true)
  assert.deepEqual(shape.args, [exec, result])
})

test('hookArgs: tools/pre-execute expands to (exec, next)', () => {
  const exec = { name: 'bash' }
  const shape = hookArgsFor('tools/pre-execute', { exec })
  assert.equal(shape.injectsNext, true)
  assert.deepEqual(shape.args, [exec])
})

test('hookArgs: tools/execute is a waterfall around-hook with (exec, next)', () => {
  const exec = { name: 'read' }
  const shape = hookArgsFor('tools/execute', { exec })
  assert.equal(shape.mode, 'waterfall')
  assert.equal(shape.injectsNext, true)
  assert.deepEqual(shape.args, [exec])
})

test('hookArgs: agent/pre-step passes payload through with next', () => {
  const payload = { agent: { id: 's1' }, messages: [{ role: 'user' }] }
  const shape = hookArgsFor('agent/pre-step', { payload })
  assert.equal(shape.injectsNext, true)
  assert.deepEqual(shape.args, [payload])
})

test('hookArgs: out-of-vocab event keeps legacy single-payload shape (no regression)', () => {
  // turn/end 是现有桥消费面（translator 词表 pre/post 边界事件），
  // 词表外保持 waterfall(event, payload, next) 旧形态不回归。
  const payload = { reason: 'x' }
  const shape = hookArgsFor('turn/end', { payload })
  assert.equal(shape.injectsNext, false)
  assert.deepEqual(shape.args, [payload])
})

test('hookArgs: emit-class events never inject next (>=2 groups)', () => {
  for (const event of ['agent/session-start', 'tools/result']) {
    const shape = hookArgsFor(event, { agent: { id: 's1' } })
    assert.equal(shape.injectsNext, false, `${event} 是 emit 类，不注入 next`)
  }
})

test('hookArgs: waterfall args never contain functions (next comes from waterfall)', () => {
  const shape = hookArgsFor('tools/post-execute', { exec: {}, result: {} })
  for (const arg of shape.args) {
    assert.equal(typeof arg, 'object', '载荷位不得混入函数（三参由 waterfall 注入）')
  }
})

test('defaultDecision: per-event vocabulary, undefined outside vocab', () => {
  assert.deepEqual(defaultDecisionFor('tools/pre-execute'), { kind: 'allow' })
  assert.deepEqual(defaultDecisionFor('tools/post-execute'), { kind: 'accept' })
  assert.equal(defaultDecisionFor('agent/pre-step'), undefined)
  assert.equal(defaultDecisionFor('turn/end'), undefined)
})

// ── 垫片三：this 代理 ──────────────────────────────────────────

test('thisArg: plain object carrying the stable agent (cordis dispatch needs object first)', () => {
  const agent = { id: 's1' }
  const ctxLike = { get: (name) => ({ name }) }
  const thisArg = makeThisArg(ctxLike, { agent })
  assert.equal(typeof thisArg, 'object')
  assert.notEqual(thisArg, null)
  assert.equal(thisArg.agent, agent, 'this.agent 必须与身份表对象同引用')
})

test('thisArg: reaches host services via ctx passthrough (>=2 groups)', () => {
  const ctxLike = { get: (name) => `svc:${name}` }
  const thisArg = makeThisArg(ctxLike, { agent: { id: 'a' } })
  assert.equal(thisArg.get('shell'), 'svc:shell')
  assert.equal(thisArg.get('tools'), 'svc:tools')
})

test('thisArg: tolerates null ctx (bridge not booted path)', () => {
  const thisArg = makeThisArg(null, { agent: { id: 'a' } })
  assert.equal(typeof thisArg.get('shell'), 'undefined')
})

// ── 垫片四：服务代理 ───────────────────────────────────────────

test('service proxy: method calls forward to rpc with (method, args)', async () => {
  const calls = []
  const rpc = async (method, args) => {
    calls.push([method, args])
    return { ok: true, data: 'r1' }
  }
  const proxy = makeServiceProxy(rpc)
  assert.equal(await proxy.loop.status(), 'r1')
  assert.deepEqual(calls[0], ['loop.status', []])
})

test('service proxy: forwards arguments positionally (>=2 methods, >=2 arg counts)', async () => {
  const calls = []
  const rpc = async (method, args) => {
    calls.push([method, args])
    return { ok: true, data: args }
  }
  const proxy = makeServiceProxy(rpc)
  assert.deepEqual(await proxy.loop.stop('task-1', { force: true }), ['task-1', { force: true }])
  assert.deepEqual(await proxy.loop.inject('task-1', 'hello', { kind: 'plugin' }), [
    'task-1', 'hello', { kind: 'plugin' },
  ])
  assert.deepEqual(calls, [
    ['loop.stop', ['task-1', { force: true }]],
    ['loop.inject', ['task-1', 'hello', { kind: 'plugin' }]],
  ])
})

test('service proxy: host-side failure propagates (no swallowing)', async () => {
  const rpc = async () => ({ ok: false, error: 'no handler' })
  const proxy = makeServiceProxy(rpc)
  await assert.rejects(() => proxy.loop.status(), /no handler/)
})

test('service proxy: rpc exception propagates untouched', async () => {
  const rpc = async () => {
    throw new Error('pipe broken')
  }
  const proxy = makeServiceProxy(rpc)
  await assert.rejects(() => proxy.loop.stop('t'), /pipe broken/)
})

// ── dispatchHook：四垫片组合发射器（桥 emitHook 的真身） ─────────

function fakeCtx() {
  const calls = []
  return {
    calls,
    async waterfall(...args) {
      calls.push(args)
      return 'SENTINEL'
    },
  }
}

test('dispatchHook: waterfall receives (thisArg, event, ...args, next) for vocab events', async () => {
  const identities = createIdentityTable()
  const ctx = fakeCtx()
  const exec = { name: 'grep', arguments: { q: 'x' } }
  const decision = await dispatchHook(ctx, identities, {
    event: 'tools/post-execute',
    exec,
    result: { ok: true },
    agentKey: 'sess-1',
  })
  assert.equal(decision, 'SENTINEL')
  assert.equal(ctx.calls.length, 1)
  const [thisArg, event, execArg, resultArg, next] = ctx.calls[0]
  assert.equal(typeof thisArg, 'object', '首参必须是 thisArg 代理（垫片三）')
  assert.equal(thisArg.agent, identities.agentFor('sess-1'), 'thisArg.agent = 身份表稳定对象（垫片一）')
  assert.equal(event, 'tools/post-execute')
  assert.equal(execArg.name, exec.name, '载荷内容原样（exec 为物化拷贝：补齐稳定 agent，其余字段照抄）')
  assert.deepEqual(execArg.arguments, exec.arguments)
  assert.equal(execArg.agent, identities.agentFor('sess-1'), 'execArg.agent = 身份表稳定对象（垫片一；插件 observe() 有 !exec.agent 守卫）')
  assert.equal(exec.agent, undefined, '调用方原 exec 引用不被就地改动（物化发生在拷贝上）')
  assert.deepEqual(resultArg, { ok: true })
  assert.equal(typeof next, 'function', '末位必须是可调用 next')
})

test('dispatchHook: next() delegates with per-event default decision', async () => {
  const ctx = fakeCtx()
  await dispatchHook(ctx, createIdentityTable(), {
    event: 'tools/pre-execute', exec: { name: 'bash' }, agentKey: 's1',
  })
  const next = ctx.calls[0][3]
  assert.deepEqual(await next(), { kind: 'allow' }, '无监听器链时 next() 落默认决策词表')
})

test('dispatchHook: next() forwards listener verdict (delegation not overwrite)', async () => {
  const ctx = fakeCtx()
  ctx.waterfall = async (...args) => {
    const next = args[args.length - 1]
    // 复刻插件链尾行为：监听器调 next() 委托并把裁决改写为 block
    return next().then((d) => ({ kind: 'block', feedback: d.kind }))
  }
  const decision = await dispatchHook(ctx, createIdentityTable(), {
    event: 'tools/post-execute', exec: {}, result: {}, agentKey: 's1',
  })
  assert.deepEqual(decision, { kind: 'block', feedback: 'accept' })
})

test('dispatchHook: legacy events bypass thisArg (bridge regression guard)', async () => {
  const ctx = fakeCtx()
  await dispatchHook(ctx, createIdentityTable(), { event: 'turn/end', payload: { r: 1 } })
  const [first, payload, next] = ctx.calls[0]
  assert.equal(first, 'turn/end', '词表外旧形态：首参直接是事件名（无 thisArg，与现有桥 waterfall(event, payload, next) 同形）')
  assert.deepEqual(payload, { r: 1 })
  assert.equal(typeof next, 'function')
})

test('dispatchHook: legacy next echoes payload (old bridge next=(value)=>value)', async () => {
  const ctx = fakeCtx()
  ctx.waterfall = async (...args) => {
    const next = args[args.length - 1]
    // 复刻 cordis 无监听器行为：以载荷调最内层 next（lib/index.js:317-325）
    return next(args[1])
  }
  const decision = await dispatchHook(ctx, createIdentityTable(), { event: 'turn/end', payload: { r: 1 } })
  assert.deepEqual(decision, { r: 1 }, '词表外无监听器：decision 回显 payload（旧桥同语义，零回归）')
})

test('dispatchHook: legacy next without arg falls back to vocab default', async () => {
  const ctx = fakeCtx()
  ctx.waterfall = async (...args) => (args[args.length - 1])()
  const decision = await dispatchHook(ctx, createIdentityTable(), { event: 'turn/end', payload: {} })
  assert.equal(decision, undefined, '词表外 next() 无实参：回落词表默认（=undefined，原样返回）')
})

test('dispatchHook: missing event fails visible', async () => {
  await assert.rejects(
    () => dispatchHook(fakeCtx(), createIdentityTable(), { payload: {} }),
    /event/,
  )
})
