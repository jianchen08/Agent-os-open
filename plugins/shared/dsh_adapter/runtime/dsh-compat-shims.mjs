// @feature: FP-0.2.可观测性 dsh_adapter DSH兼容四件垫片 | @vision: V3 可嵌入
/**
 * DSH 兼容四件垫片（P0）——让真实 DSH 插件零改动装上 AgentOS 即可识别与干预 loop。
 *
 * 设计真值：docs/working/dad914f83c1e_DSH兼容层可行性验证.md §2.7 条件集
 * （探针 1/2/4/5b/6 实证，不需要第二套宿主，缺的是这四件）：
 *   1. 身份表：agentKey/callKey → 稳定对象，保住插件侧 WeakMap 键语义；
 *   2. 三参签名：tools/* 钩子按 DSH 真实签名 (exec[, result], next) 展开，
 *      next() 可委托可改写，无监听器时落封闭决策词表；
 *   3. this 代理：waterfall 首参传对象（cordis dispatch 把对象首参当
 *      thisArg——lib/index.js:258-264），监听器内 this 指向可达宿主服务面的代理；
 *   4. 服务代理：ctx.agentos.loop.{status,stop,inject} 方法调用经 rpc() 转发
 *      到宿主（AgentOS），失败原样传播（fail-visible，禁吞）。
 *
 * 边界（P0 不做，见报告 §4「根本不可等价」）：UI slot 组件直递、code-mode、
 * session/event append-only 不变量、ctx.tools.guard（同步语义）。
 * 本模块纯逻辑、零依赖（不 import cordis/DSH 包），桥 dsh-rpc-bridge.mjs 单向
 * import；测试 plugins/shared/dsh_adapter/runtime/test/。
 */

/** tools/* 钩子的 DSH 真实签名表（rc8 契约，探针 2 实证破坏形态）。
 *  - tools/post-execute(exec, result, next) — packages/core/tools/src/index.ts:175
 *  - tools/pre-execute(exec, next) / tools/execute(exec, next) — 同文件 :152
 *  - agent/pre-step(payload, next) — runtime-types.ts:231
 *  其余事件保持旧形态 waterfall(event, payload, next)（现有桥消费面不回归）。 */
const WATERFALL_SHAPES = {
  'tools/post-execute': ['exec', 'result'],
  'tools/pre-execute': ['exec'],
  'tools/execute': ['exec'],
  'agent/pre-step': ['payload'],
}

/** 无监听器链时 next() 的封闭决策词表（报告 §2.6：决策词表封闭）。
 *  词表外事件返回 undefined（= 无默认决策，waterfall 原样返回）。 */
const DEFAULT_DECISIONS = {
  'tools/pre-execute': { kind: 'allow' },
  'tools/post-execute': { kind: 'accept' },
}

/**
 * 身份表（垫片一）：跨桥对象稳定 id → 本进程内稳定对象引用。
 * 插件不需要传递身份——只需要我们在本进程内保住身份（探针 1/4 实证：
 * 每帧新对象使 WeakMap 链计数失效，身份表下成立）。
 */
export function createIdentityTable () {
  const agents = new Map()
  const execs = new Map()
  return {
    /** agentKey → 稳定 agent 对象（WeakMap 键语义的承载体）。 */
    agentFor (agentKey) {
      if (typeof agentKey !== 'string' || agentKey === '') {
        throw new TypeError(`identity: agentKey must be a non-empty string, got ${String(agentKey)}`)
      }
      let agent = agents.get(agentKey)
      if (agent === undefined) {
        agent = { id: agentKey }
        agents.set(agentKey, agent)
      }
      return agent
    },
    /** callKey → 稳定 exec 对象（factory 只在首次构建时调用一次）。 */
    execFor (callKey, factory) {
      if (typeof callKey !== 'string' || callKey === '') {
        throw new TypeError(`identity: callKey must be a non-empty string, got ${String(callKey)}`)
      }
      let exec = execs.get(callKey)
      if (exec === undefined) {
        // 有界缓存（FIFO 驱逐最旧 1024 条）：exec 表按调用粒度增长，长会话下
        // 不设界会无界膨胀。妥协说明：exec 键语义的插件在 >1024 个未重复调用
        // 后可能看到最旧身份被驱逐（agent 键不受影响——真实插件的观测键是
        // agent）；触发升级条件=出现以 exec 为 WeakMap 键的插件且会话调用数
        // 常态超界，届时改按会话分桶或与调用生命周期挂钩。
        if (execs.size >= 1024) {
          const oldest = execs.keys().next().value
          if (oldest !== undefined) execs.delete(oldest)
        }
        exec = factory()
        execs.set(callKey, exec)
      }
      return exec
    },
  }
}

/**
 * 垫片二（参数展开）：事件名 + 载荷 → waterfall 监听器实参表。
 * injectsNext=false 表示 emit 类事件（无 next 位）；载荷位不含函数——
 * 三参由 cordis waterfall 自行注入（lib/index.js:317-325：末位实参=最内层 next）。
 */
export function hookArgsFor (event, input = {}) {
  const shape = WATERFALL_SHAPES[event]
  if (shape === undefined) {
    // 词表外（如 turn/end 等现有桥消费面）：旧形态单载荷，不注入 next 位
    return { mode: 'waterfall', injectsNext: false, args: [input.payload ?? {}] }
  }
  const args = []
  for (const slot of shape) {
    const value = input[slot]
    if (value !== undefined && value !== null) args.push(value)
  }
  if (args.length === 0) args.push({})
  return { mode: 'waterfall', injectsNext: true, args }
}

/** 无监听器时 next() 应落的默认决策（词表封闭；词表外 undefined）。 */
export function defaultDecisionFor (event) {
  return DEFAULT_DECISIONS[event]
}

/**
 * 垫片三（this 代理）：监听器内 this 指向的对象。
 * cordis dispatch 把对象首参当 thisArg；代理携带稳定 agent 引用 +
 * ctx 服务读取面（Scoped 过滤面不在 P0——见报告 §6.5 边界）。
 */
export function makeThisArg (ctx, { agent = null } = {}) {
  return {
    agent,
    get (name) {
      try {
        return ctx?.get?.(name)
      } catch {
        return undefined
      }
    },
  }
}

/**
 * 垫片四（服务代理）：AgentOS 服务面 → cordis service。
 * 插件以 DSH 习惯调用（inject: ['agentos'] + ctx.agentos.loop.*）；
 * rpc(method, args) 由桥注入（stdio 反向 RPC）。宿主失败/异常原样传播。
 */
export function makeServiceProxy (rpc) {
  const call = (method) => async (...args) => {
    const reply = await rpc(method, args)
    if (reply && reply.ok === false) {
      throw new Error(reply.error || `agentos ${method} failed`)
    }
    return reply?.data
  }
  return {
    loop: {
      status: call('loop.status'),
      stop: call('loop.stop'),
      inject: call('loop.inject'),
    },
  }
}

/**
 * 四垫片组合发射器（桥 emitHook 的真身）：
 *   词表内 waterfall 事件 → ctx.waterfall(thisArg, event, ...载荷, next)
 *   词表外旧形态       → ctx.waterfall(event, payload, next)（无 thisArg，桥回归护栏）
 *   emit 类事件        → ctx.emit(thisArg, event, ...载荷)（返回 undefined）
 * exec 载荷就地物化稳定 agent（插件 observe() 有 !exec.agent 守卫——
 * lib/index.js:2224；不物化则 WeakMap 永不命中）。
 */
export async function dispatchHook (ctx, identities, input = {}) {
  const event = input?.event
  if (typeof event !== 'string' || event === '') {
    throw new TypeError('dispatchHook: missing event')
  }
  let exec = input.exec
  const agentKey = typeof input.agentKey === 'string' && input.agentKey !== '' ? input.agentKey : null
  const stableAgent = agentKey !== null ? identities.agentFor(agentKey) : null
  if (exec !== undefined && exec !== null && typeof exec === 'object') {
    const callKey = typeof exec.callId === 'string' && exec.callId !== '' ? exec.callId : null
    const materializeAgent = (candidate) => {
      const copy = { ...candidate }
      if (copy.agent === undefined || copy.agent === null || typeof copy.agent === 'string') {
        copy.agent = stableAgent ?? identities.agentFor(
          typeof copy.agent === 'string' ? copy.agent : (callKey ?? `exec:${candidate.name ?? 'unknown'}`),
        )
      }
      return copy
    }
    if (callKey !== null) {
      // 有 callId：exec 本身也进身份表（WeakMap<exec> 类插件可用；factory 只跑一次）
      exec = identities.execFor(callKey, () => materializeAgent(exec))
    } else {
      // 无 callId：就地物化 agent——插件观测键是 exec.agent（chains.get(exec.agent)），
      // agent 物化不依赖 callId 存在
      exec = materializeAgent(exec)
    }
  }
  const shape = hookArgsFor(event, { ...input, exec })
  if (!shape.injectsNext) {
    // 词表外旧形态：无 thisArg（现有桥消费面 waterfall(event, payload, next) 同形），
    // next 回显实参（旧桥 next=(value)=>value 同语义：cordis 无监听器时以载荷调
    // 最内层 next、链尾显式 next(x) 时 decision=x；无实参回落词表默认——词表外
    // =undefined 原样返回）——emit 风格监听器忽略尾部 next，行为兼容
    const legacyNext = (value) => Promise.resolve(value !== undefined ? value : defaultDecisionFor(event))
    return await ctx.waterfall(event, ...shape.args, legacyNext)
  }
  const next = () => Promise.resolve(defaultDecisionFor(event))
  return await ctx.waterfall(makeThisArg(ctx, { agent: stableAgent }), event, ...shape.args, next)
}
