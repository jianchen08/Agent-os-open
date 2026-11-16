#!/usr/bin/env node
/**
 * AgentOS ↔ DSH 工具桥（通道 A：npm 运行时工作区 boot）。
 *
 * boot 落位（设计 §4.2 C2）：从**运行时工作区**（用户空间 npm 项目，env
 * ``AGENTOS_DSH_RUNTIME_ROOT`` 指向）解析 ``@deepseek-ai/*`` 基础包与已装
 * 插件包的 node_modules，boot 一个按 ``bootPlan`` 裁剪的最小 cordis context。
 * 不依赖任何 DSH 仓库产物路径（rc8 参考仓已降级为测试夹具）。
 *
 * boot 裁剪轴（AC-R4，initialize params.bootPlan）：
 *   { chain: bool,            // 最小工具链（SystemPrompt→ToolRuntime→LocalFS→Subprocess→ToolFs/ToolFsSearch）
 *     webServer: bool,        // P1 轴（服务类包；本轮恒 false，信号面在 Python 侧）
 *     hooks: bool,            // P1 轴（钩子拦截面；同上）
 *     packages: [{name, entry}] }  // 已启用插件包的入口（绝对路径，file URL 装载）
 * chain=false（空集）→ 不 import 任何 DSH 包（含 cordis），initialize 返回空
 * 工具清单——空工作区 boot 不加载任何 DSH 包。
 *
 * 协议（newline-delimited JSON-RPC，stdout 专属协议帧，日志一律走 stderr）：
 *   initialize {cwd, bootPlan?}  → {serverInfo, tools: [{name, description, input_schema, output_schema}]}
 *   tool/call {name, args, timeoutMs?} → {success, data, error, duration_ms}
 *   service/call {name, method, args, timeoutMs?} → {success, data, error, duration_ms}
 *     （设计 §4.3 AC-X1：DSH Service 实例方法经桥调用；服务由已装包在 boot
 *     context 内提供，形如 ctx.<name>.<method>()）
 *   hook/emit {event, payload} → {ok, event, decision, error?}
 *     （AC-X3：把管道边界事件送回 DSH 拦截点监听器；waterfall 组合语义，
 *     payload 原样进出，监听器签名不匹配时如实报错）
 *   http/request {method, path, headers?, body_base64?} → {status, headers, body_base64}
 *     （AC-X2：经 boot 的 webServer 实际监听器发内网请求；webServer 轴未装=ok:false）
 *   duration_ms 为整数毫秒：内核信封按 Option<u64> 严格反序列化，小数位
 *   （如 0.0）会整条判 PARSE_ERROR。
 *   shutdown                     → {} （dispose 后 exit 0）
 *
 * MIT 出处：DSH 各包 MIT License, Copyright (c) 2026 DeepSeek；本文件是
 * AgentOS 侧新增的宿主代码（非 DSH 复制）。
 */

import { createRequire } from 'node:module'
import { createInterface } from 'node:readline'
import { existsSync } from 'node:fs'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'

const RUNTIME_ROOT = (process.env.AGENTOS_DSH_RUNTIME_ROOT ?? '').trim()

let ctx = null
let booted = false
let webServerOrigin = null
let webServerError = null

/** 从工作区 node_modules 解析包入口（createRequire 锚定工作区 package.json）。 */
function wsResolve (spec) {
  if (!RUNTIME_ROOT) {
    throw new Error('AGENTOS_DSH_RUNTIME_ROOT not configured (user-space npm runtime workspace required)')
  }
  const req = createRequire(join(RUNTIME_ROOT, 'package.json'))
  return pathToFileURL(req.resolve(spec)).href
}

async function boot (cwd, plan) {
  const chain = plan ? plan.chain !== false : true
  if (!chain) {
    // 空集：不 boot、不加载任何 DSH 包（AC-R4）。
    booted = true
    return
  }
  const { Context } = await import(wsResolve('@deepseek-ai/cordis'))
  const SystemPrompt = (await import(wsResolve('@deepseek-ai/dsh-system-prompt'))).default
  const { ToolRuntime } = await import(wsResolve('@deepseek-ai/dsh-tools'))
  const LocalFileSystem = (await import(wsResolve('@deepseek-ai/dsh-fs-local'))).default
  const SubprocessLocal = (await import(wsResolve('@deepseek-ai/dsh-subprocess-local'))).default
  const SessionProjection = (await import(wsResolve('@deepseek-ai/dsh-session-projection'))).default
  const WorkingDirectory = (await import(wsResolve('@deepseek-ai/dsh-working-directory'))).default
  const ToolFs = await import(wsResolve('@deepseek-ai/dsh-tool-fs'))
  const ToolFsSearch = await import(wsResolve('@deepseek-ai/dsh-tool-fs-search'))

  ctx = new Context()
  await ctx.plugin(SystemPrompt)
  await ctx.plugin(ToolRuntime)
  await ctx.plugin(LocalFileSystem, { cwd })
  await ctx.plugin(SubprocessLocal)
  // 0.2.1 链：tool-fs 的 inject 需 workingDirectory → 需 sessionProjections
  // （dsh-working-directory 的 defaultDirectory 必须绝对路径，取 boot cwd）
  await ctx.plugin(SessionProjection)
  await ctx.plugin(WorkingDirectory, { defaultDirectory: cwd })
  await ctx.plugin(ToolFs)
  await ctx.plugin(ToolFsSearch, { sampleOverCapGlobResults: true })
  if (plan?.webServer === true) {
    await bootWebServer()
  }
  await loadPlanPackages(plan?.packages)
  booted = true
}

/** webServer 轴（AC-X2）：装载 dsh-host-webserver（真实 HTTP 监听器），
 *  记录本地 origin 供 http/request 反代。包未装 = 轴不可用但**不拖垮工具
 *  面**：错误记入 serverInfo.webServerError（fail-visible，代理端点 502 带
 *  原因），工具调用照常。 */
async function bootWebServer () {
  let WebServer
  try {
    WebServer = (await import(wsResolve('@deepseek-ai/dsh-host-webserver'))).WebServer
  } catch (err) {
    webServerError = `webServer 轴需要 @deepseek-ai/dsh-host-webserver（运行时工作区未安装）: ${err instanceof Error ? err.message : err}`
    process.stderr.write(`[dsh-bridge] ${webServerError}\n`)
    return
  }
  if (typeof WebServer !== 'function') {
    webServerError = '@deepseek-ai/dsh-host-webserver 未导出 WebServer 类'
    process.stderr.write(`[dsh-bridge] ${webServerError}\n`)
    return
  }
  try {
    await ctx.plugin(WebServer, { host: '127.0.0.1', port: 0 })
    webServerOrigin = ctx.webServer?.getOrigin?.() ?? null
    if (!webServerOrigin) {
      webServerError = 'webServer 服务未提供 getOrigin()——dsh-host-webserver 契约漂移'
      process.stderr.write(`[dsh-bridge] ${webServerError}\n`)
      return
    }
    process.stderr.write(`[dsh-bridge] webServer origin: ${webServerOrigin}\n`)
  } catch (err) {
    webServerError = `webServer 装载失败: ${err instanceof Error ? err.message : err}`
    process.stderr.write(`[dsh-bridge] ${webServerError}\n`)
  }
}

/** 装载 bootPlan.packages 里的插件包（工作区 node_modules 中的已启用包；
 * 无 apply 形态的包如实跳过并留痕，不静默）。 */
async function loadPlanPackages (packages) {
  for (const pkg of packages ?? []) {
    const entry = typeof pkg?.entry === 'string' ? pkg.entry : ''
    if (!entry || !existsSync(entry)) {
      process.stderr.write(`[dsh-bridge] plan package ${pkg?.name ?? '?'} entry missing: ${entry}\n`)
      continue
    }
    try {
      const mod = await import(pathToFileURL(entry).href)
      if (typeof mod?.apply !== 'function') {
        process.stderr.write(`[dsh-bridge] plan package ${pkg.name ?? '?'} has no apply, skipped\n`)
        continue
      }
      await ctx.plugin(mod)
      process.stderr.write(`[dsh-bridge] plan package loaded: ${pkg.name ?? '?'}\n`)
    } catch (err) {
      process.stderr.write(`[dsh-bridge] plan package ${pkg.name ?? '?'} load failed: ${err instanceof Error ? err.message : err}\n`)
    }
  }
}

/** DSH parameters DSL（{field: {type, required?, description?}}）→ JSON Schema。 */
function paramsToJsonSchema (parameters) {
  if (parameters === undefined || parameters === null) return { type: 'object', properties: {} }
  const properties = {}
  const required = []
  for (const [key, spec] of Object.entries(parameters)) {
    properties[key] = {
      type: spec.type,
      ...(spec.description !== undefined ? { description: spec.description } : {}),
    }
    if (spec.required === true) required.push(key)
  }
  return { type: 'object', properties, ...(required.length > 0 ? { required } : {}) }
}

/** 导出已注册工具的模型面契约（presentation 回调不下发——那是宿主侧纯函数）。 */
function listTools () {
  const out = []
  let entries = []
  try {
    // 动态枚举：boot 插件（read/glob/grep）+ 计划装载的工具包全部可见
    entries = [...(ctx.tools.view().visible.entries())]
  } catch {
    entries = ['read', 'glob', 'grep'].map((n) => [n, ctx.tools.get(n)]).filter(([, t]) => t !== undefined)
  }
  for (const [, tool] of entries) {
    if (tool === undefined) continue
    out.push({
      name: tool.name,
      description: tool.description ?? '',
      input_schema: paramsToJsonSchema(tool.parameters),
      output_schema: tool.output?.schema ?? null,
    })
  }
  return out
}

/** 构造直接执行所需的最小 ToolRunContext（跳过 agent/审批管道——桥接工具
 *  的准入由灵汐侧 isolation_guard/security 统一把关，DSH 侧不再叠加策略）。 */
function makeExec (callId, name, args, signal) {
  return {
    callId,
    rootCallId: callId,
    name,
    arguments: args,
    signal,
    deferContext() {},
    concludeTurn() {},
  }
}

async function callTool (id, params) {
  const name = String(params?.name ?? '')
  const args = params?.args ?? {}
  const timeoutMs = Number.isFinite(params?.timeoutMs) ? params.timeoutMs : 120_000
  const tool = booted && ctx !== null ? ctx.tools.get(name) : undefined
  if (tool === undefined) {
    return {
      jsonrpc: '2.0',
      id,
      result: { success: false, data: null, error: `unknown tool: ${name}`, duration_ms: 0 },
    }
  }
  const controller = new AbortController()
  const timer = setTimeout(() => { controller.abort() }, timeoutMs)
  const start = performance.now()
  try {
    const exec = makeExec(`agentos-${id}`, name, args, controller.signal)
    const value = await tool.execute(args, exec)
    return {
      jsonrpc: '2.0',
      id,
      result: {
        success: true,
        data: value,
        error: null,
        duration_ms: Math.round(performance.now() - start),
      },
    }
  } catch (err) {
    return {
      jsonrpc: '2.0',
      id,
      result: {
        success: false,
        data: null,
        error: err instanceof Error ? `${err.name}: ${err.message}` : String(err),
        duration_ms: Math.round(performance.now() - start),
      },
    }
  } finally {
    clearTimeout(timer)
  }
}

async function callService (id, params) {
  const name = String(params?.name ?? '')
  const method = String(params?.method ?? '')
  const args = Array.isArray(params?.args) ? params.args : []
  const timeoutMs = Number.isFinite(params?.timeoutMs) ? params.timeoutMs : 120_000
  const start = performance.now()
  const fail = (error) => ({
    jsonrpc: '2.0',
    id,
    result: { success: false, data: null, error, duration_ms: Math.round(performance.now() - start) },
  })
  if (!booted || ctx === null) return fail('bridge not initialized')
  const service = ctx[name]
  if (service === undefined || service === null) return fail(`unknown service: ${name}`)
  const fn = service[method]
  if (typeof fn !== 'function') return fail(`unknown method: ${name}.${method}`)
  let timer
  try {
    const timeout = new Promise((_, reject) => {
      timer = setTimeout(() => { reject(new Error(`service call timeout after ${timeoutMs}ms`)) }, timeoutMs)
    })
    const value = await Promise.race([fn.apply(service, args), timeout])
    return {
      jsonrpc: '2.0',
      id,
      result: { success: true, data: value ?? null, error: null, duration_ms: Math.round(performance.now() - start) },
    }
  } catch (err) {
    return fail(err instanceof Error ? `${err.name}: ${err.message}` : String(err))
  } finally {
    clearTimeout(timer)
  }
}

/** 拦截点事件送回 DSH 监听器（waterfall 组合：payload 原样进出）。 */
function emitHook (id, params) {
  const event = String(params?.event ?? '')
  const payload = params?.payload ?? {}
  if (!event) {
    return { jsonrpc: '2.0', id, result: { ok: false, event, decision: null, error: 'missing event' } }
  }
  if (!booted || ctx === null) {
    return { jsonrpc: '2.0', id, result: { ok: false, event, decision: null, error: 'bridge not initialized' } }
  }
  try {
    const next = (value) => value
    const decision = ctx.waterfall(event, payload, next)
    return {
      jsonrpc: '2.0',
      id,
      result: { ok: true, event, decision: decision === undefined ? null : decision, error: null },
    }
  } catch (err) {
    return {
      jsonrpc: '2.0',
      id,
      result: {
        ok: false,
        event,
        decision: null,
        error: err instanceof Error ? `${err.name}: ${err.message}` : String(err),
      },
    }
  }
}

/** 经 boot 的 webServer 实际监听器发请求（AC-X2 反代的传输面）。 */
async function httpRequest (id, params) {
  const method = String(params?.method ?? 'GET').toUpperCase()
  const path = String(params?.path ?? '/')
  const headers = params?.headers && typeof params.headers === 'object' ? params.headers : {}
  const start = performance.now()
  const fail = (error) => ({
    jsonrpc: '2.0',
    id,
    result: { ok: false, status: 0, headers: {}, body_base64: '', error, duration_ms: Math.round(performance.now() - start) },
  })
  if (!webServerOrigin) return fail(webServerError || 'webServer service unavailable (boot plan webServer=false)')
  if (!path.startsWith('/')) return fail(`path must be absolute: ${path}`)
  try {
    const body = typeof params?.body_base64 === 'string' && params.body_base64 !== ''
      ? Buffer.from(params.body_base64, 'base64')
      : undefined
    const resp = await fetch(new URL(path, webServerOrigin), { method, headers, body })
    const buf = Buffer.from(await resp.arrayBuffer())
    const outHeaders = {}
    resp.headers.forEach((value, key) => { outHeaders[key] = value })
    return {
      jsonrpc: '2.0',
      id,
      result: {
        ok: true,
        status: resp.status,
        headers: outHeaders,
        body_base64: buf.toString('base64'),
        error: null,
        duration_ms: Math.round(performance.now() - start),
      },
    }
  } catch (err) {
    return fail(err instanceof Error ? `${err.name}: ${err.message}` : String(err))
  }
}

async function handleRequest (method, params) {
  switch (method) {
    case 'initialize': {
      const cwd = typeof params?.cwd === 'string' && params.cwd !== '' ? params.cwd : process.cwd()
      await boot(cwd, params?.bootPlan)
      return {
        serverInfo: {
          name: 'agentos-dsh-bridge',
          version: '0.2.1',
          webServerOrigin,
          webServerError,
        },
        tools: ctx !== null ? listTools() : [],
      }
    }
    case 'tools/list': {
      if (!booted || ctx === null) return { tools: [] }
      return { tools: listTools() }
    }
    case 'service/call':
    case 'hook/emit':
    case 'http/request': {
      throw new Error(`${method} must be dispatched by the frame loop`)
    }
    case 'shutdown': {
      if (booted && ctx !== null) {
        try { await ctx.dispose() } catch { /* 退出路径上的清理失败不阻塞 */ }
      }
      return {}
    }
    default:
      throw new Error(`unknown method: ${method}`)
  }
}

// stdin 行协议主循环（stdout 只写协议帧）。
const rl = createInterface({ input: process.stdin })
rl.on('line', (line) => {
  const text = line.trim()
  if (text === '') return
  let msg
  try {
    msg = JSON.parse(text)
  } catch {
    process.stderr.write(`[dsh-bridge] bad frame: ${text.slice(0, 120)}\n`)
    return
  }
  void (async () => {
    let response
    if (msg.method === 'tool/call') {
      response = await callTool(msg.id, msg.params)
    } else if (msg.method === 'service/call') {
      response = await callService(msg.id, msg.params)
    } else if (msg.method === 'hook/emit') {
      response = emitHook(msg.id, msg.params)
    } else if (msg.method === 'http/request') {
      response = await httpRequest(msg.id, msg.params)
    } else {
      try {
        const result = await handleRequest(msg.method, msg.params)
        response = { jsonrpc: '2.0', id: msg.id, result }
        if (msg.method === 'shutdown') {
          process.stdout.write(`${JSON.stringify(response)}\n`)
          process.exit(0)
        }
      } catch (err) {
        response = {
          jsonrpc: '2.0',
          id: msg.id,
          error: { code: -32000, message: err instanceof Error ? err.message : String(err) },
        }
      }
    }
    process.stdout.write(`${JSON.stringify(response)}\n`)
  })()
})

process.stderr.write(`[dsh-bridge] ready (workspace=${RUNTIME_ROOT || 'not-configured'})\n`)
