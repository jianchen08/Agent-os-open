#!/usr/bin/env node
/**
 * AgentOS ↔ DSH 工具桥（通道 A：DSH profile 运行时工作区 boot）。
 *
 * boot 落位（设计 §4.2 C2）：从**运行时工作区**（用户空间 DSH profile，env
 * ``AGENTOS_DSH_RUNTIME_ROOT`` 指向）解析 ``@deepseek-ai/*`` 基础包与已装
 * bundle 的 node_modules，boot 一个按 ``bootPlan`` 裁剪的最小 cordis context。
 * 不依赖任何 DSH 仓库产物路径（rc8 参考仓已降级为测试夹具）。
 *
 * 管理面（2026-11-09 起）：同一进程内另起一个 **management context**，挂
 * 官方 ``@deepseek-ai/dsh-plugin-manager``（Loader + profileContext）；安装/
 * 卸载/启停全部委托官方 manager（pnpm + profile 语义），工具链 context 不被
 * Loader/manager 污染。变更返回 ``restart-required``，Python 侧重启桥生效。
 *
 * boot 裁剪轴（AC-R4，initialize params.bootPlan）：
 *   { source: "profile",      // 官方 bundle patch composition 优先
 *     fallback: {chain, packages},  // composition 失败/旧工作区回退
 *     chain: bool,            // 最小工具链（SystemPrompt→ToolRuntime→LocalFS→Subprocess→ToolFs/ToolFsSearch）
 *     webServer: bool,        // P1 轴（服务类包；本轮恒 false，信号面在 Python 侧）
 *     hooks: bool,            // P1 轴（钩子拦截面；同上）
 *     packages: [{name, entry}] }  // 已启用插件包的入口（绝对路径，file URL 装载）
 * chain=false（空集）→ 不 import 工具链包，initialize 返回空工具清单。
 *
 * 协议（newline-delimited JSON-RPC，stdout 专属协议帧，日志一律走 stderr）：
 *   initialize {cwd, bootPlan?, manager?} → {serverInfo, tools: [...]}
 *   tool/call {name, args, timeoutMs?} → {success, data, error, duration_ms}
 *   service/call {name, method, args, timeoutMs?} → {success, data, error, duration_ms}
 *   hook/emit {event, payload} → {ok, event, decision, error?}
 *   http/request {method, path, headers?, body_base64?} → {status, headers, body_base64}
 *   plugin/manager/{list,inspect,install,cancel,remove,enable,disable,registries}
 *       → {ok, data, error}（官方 manager 的 ChangeResult 原样返回）
 *   plugin/host-dep/add {specs} → {ok, data: PackageResult, error}
 *       （复用官方 runProfilePnpm 补装宿主依赖，不写 profile bundles）
 *   initialize 的 serverInfo.bundleStates = [{id, name, state, label, injects}]
 *       （profile 各 bundle 行装载后的 cordis Fiber 状态：pending=待注入服务、
 *       active=已激活、failed=装载失败；管理页据此区分「装了/加载了/激活了」）
 *   initialize 的 serverInfo.providerStates = [{name, state, label, injects, error?}]
 *       （可选宿主服务提供包挂载态：timer/storage/attachments/credentials/llm/
 *       settings/commands/connection/agentDefaultModel；未安装跳过=不出现在表里）
 *   duration_ms 为整数毫秒：内核信封按 Option<u64> 严格反序列化，小数位
 *   （如 0.0）会整条判 PARSE_ERROR。
 *   shutdown                     → {} （dispose 后 exit 0）
 *
 * MIT 出处：DSH 各包 MIT License, Copyright (c) 2026 DeepSeek；本文件是
 * AgentOS 侧新增的宿主代码（非 DSH 复制）。
 */

import { exec, execFile } from 'node:child_process'
import { createRequire } from 'node:module'
import { createInterface } from 'node:readline'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'

// DSH 兼容四件垫片（P0）：身份表 / 三参签名 / this 代理 / 服务代理。
// 设计真值：docs/working/dad914f83c1e_DSH兼容层可行性验证.md §2.7。
import {
  createIdentityTable,
  dispatchHook,
  makeServiceProxy,
} from './dsh-compat-shims.mjs'

/** 身份表（垫片一）：agentKey/callKey → 稳定对象，桥进程级唯一。 */
const identities = createIdentityTable()

/** Node→Python 反向 RPC（垫片四）：服务代理的传输面。下行请求 id 为 int，
 * 上行请求 id 为 "up-N" 字符串——同一 stdout/stdin 双向复用，命名空间天然隔离。 */
const pendingUpstream = new Map()
let upstreamSeq = 0
const UPSTREAM_TIMEOUT_MS = 30_000

function rpcUpstream (method, args) {
  return new Promise((resolvePromise) => {
    const id = `up-${++upstreamSeq}`
    const timer = setTimeout(() => {
      pendingUpstream.delete(id)
      resolvePromise({ ok: false, error: `agentos ${method} timeout after ${UPSTREAM_TIMEOUT_MS}ms` })
    }, UPSTREAM_TIMEOUT_MS)
    timer.unref?.()
    pendingUpstream.set(id, (reply) => {
      clearTimeout(timer)
      pendingUpstream.delete(id)
      resolvePromise(reply)
    })
    process.stdout.write(`${JSON.stringify({ jsonrpc: '2.0', id, method: 'agentos/call', params: { method, args } })}\n`)
  })
}

const RUNTIME_ROOT = (process.env.AGENTOS_DSH_RUNTIME_ROOT ?? '').trim()

// stdout 是 JSON-RPC 帧通道：插件里的 console.log/info/debug 会写出非帧文本，
// 读端只能按 bad frame 丢弃（实测 dsh-cost-meter 加载即打印三行）。统一重定向
// 到 stderr——诊断仍可见，帧通道不被污染。
for (const level of ['log', 'info', 'debug']) {
  console[level] = (...args) => console.error(...args)
}

let ctx = null
let managerCtx = null
let pluginManager = null
let managerError = null
let managerConfigGlobal = null
let profileContext = null
let booted = false
let webServerOrigin = null
let webServerError = null
/** profile 各 bundle 行装载后的 cordis Fiber 状态（诊断「加载但未激活」）。 */
let bundleStates = []
/** 可选宿主服务提供包的挂载态（timer/storage/attachments/credentials/llm/commands…）。 */
let providerStates = []
/** boot cwd（= 灵汐会话工作区的 DSH 侧投影）。 */
let bootCwd = ''
/** 当前生效的模型选择（灵汐 llm.yaml 翻译 + DSH provider 兜底；read_image 路由用）。 */
let activeModel = null

/** FiberState 数值 → 稳定标签（cordis v4：PENDING=0..UNLOADING=5）。 */
const FIBER_STATE_LABELS = {
  0: 'pending', 1: 'loading', 2: 'active', 3: 'failed', 4: 'disposed', 5: 'unloading',
}

function fiberStateOf (fiber) {
  const state = typeof fiber?.state === 'number' ? fiber.state : null
  let injects = []
  try {
    if (fiber?.inject !== null && typeof fiber?.inject === 'object') {
      injects = Object.keys(fiber.inject)
    }
  } catch {
    injects = []
  }
  return {
    state,
    label: state === null ? 'unknown' : (FIBER_STATE_LABELS[state] ?? `state-${state}`),
    injects,
  }
}

const BASE_CHAIN_MODULES = new Set([
  '@deepseek-ai/cordis',
  '@deepseek-ai/dsh-system-prompt',
  '@deepseek-ai/dsh-tools',
  '@deepseek-ai/dsh-fs-local',
  '@deepseek-ai/dsh-subprocess-local',
  '@deepseek-ai/dsh-session',
  '@deepseek-ai/dsh-session-projection',
  '@deepseek-ai/dsh-scope',
  '@deepseek-ai/dsh-working-directory',
  '@deepseek-ai/dsh-tool-fs',
  '@deepseek-ai/dsh-tool-fs-search',
  '@deepseek-ai/dsh-host-webserver',
])

/** 从工作区 node_modules 解析包入口（createRequire 锚定工作区 package.json）。 */
function wsResolve (spec) {
  if (!RUNTIME_ROOT) {
    throw new Error('AGENTOS_DSH_RUNTIME_ROOT not configured (user-space npm runtime workspace required)')
  }
  const req = createRequire(join(RUNTIME_ROOT, 'package.json'))
  return pathToFileURL(req.resolve(spec)).href
}

async function boot (cwd, plan, managerConfig) {
  const chain = plan === undefined || plan === null
    ? true
    : (plan.chain === undefined ? plan.source !== 'profile' : plan.chain !== false)
  bootCwd = cwd
  profileContext = buildProfileContext(cwd, managerConfig)
  managerConfigGlobal = managerConfig ?? null
  bundleStates = []
  providerStates = []
  if (chain) await bootChain(cwd, plan)
  await bootManager(managerConfig)
  if (plan?.source === 'profile') {
    await loadProfilePackages(plan)
  } else {
    await loadPlanPackages(plan?.packages)
  }
  // 让延后注册（setTimeout(0) 挂工具，如 @zzerx/dsh-plugin-memory）在
  // initialize 返回工具清单前落地：10ms 让位（timers 相位先于后续宏任务）。
  await new Promise((resolve) => setTimeout(resolve, 10))
  booted = true
}

/**
 * DSH ``web`` 服务 → 本机 fetch（灵汐网络能力翻译）。返回 DSH 抓取契约：
 * ``{url, statusCode, truncated, body: {kind, content}}``。
 */
function makeWebService () {
  return {
    async fetch (request, signal) {
      const url = String(request?.url ?? '')
      if (!/^https?:\/\//i.test(url)) {
        throw new Error(`web.fetch: unsupported url ${JSON.stringify(url)}`)
      }
      const response = await fetch(url, {
        redirect: 'follow',
        ...(signal === undefined ? {} : { signal }),
      })
      const body = Buffer.from(await response.arrayBuffer()).toString('utf8')
      const contentType = response.headers.get('content-type') ?? ''
      return {
        url: response.url || url,
        statusCode: response.status,
        truncated: false,
        body: {
          kind: contentType.includes('html') ? 'html' : 'text',
          content: body,
        },
      }
    },
  }
}

/**
 * DSH ``shell`` 服务 → 本机进程执行（灵汐 subprocess 能力翻译）。
 * ``resolve`` 恒等；``run`` 返回 ``{exitCode, timedOut, aborted, stdout, stderr}``。
 * Windows 走 powershell -Command（argv 直传，无 shell 拼接）。
 */
function makeShellService () {
  const isWin = process.platform === 'win32'
  return {
    resolve (request) { return request },
    run (request) {
      const command = String(request?.command ?? '')
      const timeout = Number(request?.timeoutMs ?? 120_000)
      const maxBuffer = Math.max(1, Number(request?.stdoutMaxBytes ?? 10 * 1024 * 1024))
      const signal = request?.signal
      return new Promise((resolvePromise) => {
        const done = (err, stdout, stderr) => {
          const exitCode = err === null || err === undefined
            ? 0
            : (typeof err.code === 'number' ? err.code : 1)
          resolvePromise({
            exitCode,
            timedOut: Boolean(err && (err.killed === true || err.code === 'ETIMEDOUT')),
            aborted: Boolean(signal && signal.aborted === true),
            stdout: { text: String(stdout ?? ''), truncated: false },
            stderr: { text: String(stderr ?? '') },
          })
        }
        const options = {
          timeout, maxBuffer, windowsHide: true,
          ...(signal === undefined ? {} : { signal }),
        }
        try {
          if (isWin) execFile('powershell', ['-NoProfile', '-NonInteractive', '-Command', command], options, done)
          else exec(command, options, done)
        } catch (err) {
          done(err, '', '')
        }
      })
    },
  }
}

/** 工具链 context（AC-R4 最小链；与 management context 分离）。 */
async function bootChain (cwd, plan) {
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
  if (profileContext !== null) ctx.provide('profileContext', profileContext)
  // 本地服务翻译：web/shell 用灵汐本机能力背书（Node fetch / 本地进程执行），
  // 不要求插件另装宿主 provider；工具（scrape-webpage 等）按 DSH 契约调用。
  ctx.provide('web', makeWebService())
  ctx.provide('shell', makeShellService())
  // 垫片四：AgentOS 服务面 → cordis service（插件 inject: ['agentos'] 即用；
  // 方法体经 rpcUpstream 反向 RPC 转发到 Python 宿主 handler 表）。
  ctx.provide('agentos', makeServiceProxy(rpcUpstream))
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
  await bootProviders(plan)
}

/**
 * 可选宿主服务提供包（顺序=依赖序）。未安装即跳过（补装面负责装）；挂载失败
 * 只记 providerStates 并继续——服务面缺口不拖垮工具面（fail-visible）。
 * agentDefaultModel 需要配置：由 bootPlan.agentDefaultModel 传入（Python 侧把
 * 灵汐模型配置翻译成 DSH 服务配置）。
 */
const PROVIDER_PACKAGES = [
  ['@deepseek-ai/cordis-plugin-timer', {}],
  ['@deepseek-ai/dsh-storage', {}],
  // 官方本地 JSON 存储后端：root 落运行时工作区（插件数据=用户空间数据）
  ['@deepseek-ai/dsh-storage-json', { root: join(RUNTIME_ROOT, '.agentos', 'storage-json') }],
  // storageDomain 需要指名后端（json = 官方本地 JSON 存储实现）
  ['@deepseek-ai/dsh-storage-domain', { backend: 'json' }],
  // 附件用具体实现（attachment-local）；dsh-attachment 只是服务接缝基类
  ['@deepseek-ai/dsh-attachment-local', {}],
  // 凭据用文件后端具体实现（credentials-local）；dsh-credentials 只是接缝基类
  ['@deepseek-ai/dsh-credentials-local', {}],
  ['@deepseek-ai/dsh-llm', {}],
  // 用灵汐同款 DEEPSEEK_API_KEY 环境变量注册 deepseek provider（本地凭据翻译）
  ['@deepseek-ai/dsh-llm-deepseek-api-key', {}],
  ['@deepseek-ai/dsh-config-editor', {}],
  ['@deepseek-ai/dsh-settings', {}],
  ['@deepseek-ai/dsh-commands', {}],
  ['@deepseek-ai/dsh-client-connection', {}],
]

async function bootProviders (plan) {
  for (const [name, config] of PROVIDER_PACKAGES) {
    let mod
    try {
      mod = await import(wsResolve(name))
    } catch {
      continue
    }
    await mountProvider(name, mod?.default ?? mod, config)
  }
  let modelConfig = plan?.agentDefaultModel
  if (modelConfig !== null && typeof modelConfig === 'object') {
    modelConfig = await resolveDefaultModel(modelConfig)
    let mod
    try {
      mod = await import(wsResolve('@deepseek-ai/dsh-agent-default-model'))
    } catch (err) {
      providerStates.push({ name: '@deepseek-ai/dsh-agent-default-model', state: null,
        label: 'missing', injects: [], error: messageOf(err) })
      return
    }
    await mountProvider('@deepseek-ai/dsh-agent-default-model', mod?.default ?? mod, modelConfig)
  }
}

/**
 * 灵汐模型配置 → DSH 可用 provider/model：配置的 provider 不存在时退到
 * llm 服务第一个可用 provider 的首个模型（本地配置翻译 + 可用性兜底）。
 */
async function resolveDefaultModel (modelConfig) {
  activeModel = modelConfig
  try {
    const llm = ctx.get('llm')
    if (!llm || typeof llm.listProviders !== 'function') return modelConfig
    const providers = llm.listProviders()
    const ids = providers.map((p) => p?.id)
    if (ids.includes(modelConfig.provider)) {
      activeModel = modelConfig
      return modelConfig
    }
    const first = providers[0]
    if (!first) return modelConfig
    const models = typeof llm.listModels === 'function' ? await llm.listModels(first.id) : []
    const model = Array.isArray(models) ? models[0] : undefined
    if (!model) return modelConfig
    const fallback = { provider: first.id, model: model.id }
    activeModel = fallback
    process.stderr.write(`[dsh-bridge] agentDefaultModel fallback: ${fallback.provider}/${fallback.model}
`)
    return fallback
  } catch (err) {
    process.stderr.write(`[dsh-bridge] agentDefaultModel resolve failed: ${messageOf(err)}
`)
    return modelConfig
  }
}

async function mountProvider (name, plugin, config) {
  try {
    const fiber = await ctx.plugin(plugin, config)
    const info = fiberStateOf(fiber)
    providerStates.push({ name, ...info })
    if (info.label !== 'active') {
      process.stderr.write(`[dsh-bridge] provider ${name} state=${info.label}
`)
    }
  } catch (err) {
    providerStates.push({ name, state: null, label: 'failed', injects: [], error: messageOf(err) })
    process.stderr.write(`[dsh-bridge] provider ${name} mount failed: ${messageOf(err)}
`)
  }
}

/** 管理 context（官方 plugin-manager；Loader + profileContext 注入）。 */
async function bootManager (managerConfig) {
  if (!RUNTIME_ROOT) {
    managerError = 'AGENTOS_DSH_RUNTIME_ROOT not configured'
    return
  }
  ensureProfileFiles()
  let Context, Loader, PluginManager
  try {
    Context = (await import(wsResolve('@deepseek-ai/cordis'))).Context
    Loader = (await import(wsResolve('@deepseek-ai/cordis-plugin-loader'))).default
    PluginManager = (await import(wsResolve('@deepseek-ai/dsh-plugin-manager'))).default
  } catch (err) {
    managerError = `official plugin-manager unavailable (bootstrap required): ${messageOf(err)}`
    process.stderr.write(`[dsh-bridge] ${managerError}\n`)
    return
  }
  try {
    managerCtx = new Context()
    managerCtx.baseUrl = pathToFileURL(RUNTIME_ROOT).href + '/'
    await managerCtx.plugin(Loader, { baseUrl: managerCtx.baseUrl })
    managerCtx.provide('profileContext', profileContext)
    await managerCtx.plugin(PluginManager, managerPluginConfig(managerConfig))
    pluginManager = managerCtx.pluginManager ?? null
    if (pluginManager === null) {
      managerError = 'plugin-manager mounted without pluginManager service'
      process.stderr.write(`[dsh-bridge] ${managerError}\n`)
    }
  } catch (err) {
    managerError = `plugin-manager mount failed: ${messageOf(err)}`
    pluginManager = null
    process.stderr.write(`[dsh-bridge] ${managerError}\n`)
  }
}

function buildProfileContext (cwd, managerConfig) {
  if (!RUNTIME_ROOT) return null
  const pnpm = typeof managerConfig?.pnpmCommand === 'string' && managerConfig.pnpmCommand !== ''
    ? {
        command: managerConfig.pnpmCommand,
        args: Array.isArray(managerConfig.pnpmArgs) ? managerConfig.pnpmArgs : [],
        env: {},
      }
    : undefined
  return {
    name: 'agentos',
    dir: RUNTIME_ROOT,
    patchPath: join(RUNTIME_ROOT, 'cordis.patch.yml'),
    installAnchor: join(RUNTIME_ROOT, '.agentos', 'install-anchor', 'package.json'),
    cwd,
    home: join(RUNTIME_ROOT, '.dsh-home'),
    startedBundles: [],
    overlays: [],
    telemetryDisabledEnv: process.env.DSH_TELEMETRY_DISABLED,
    ...(pnpm === undefined ? {} : { packageManager: pnpm }),
  }
}

function managerPluginConfig (managerConfig) {
  const config = {}
  for (const key of [
    'pnpmCommand', 'registry', 'fallbackRegistries', 'outputBytes',
    'inspectTimeoutMs', 'idleTimeoutMs', 'lockWaitMs',
  ]) {
    if (managerConfig?.[key] !== undefined) config[key] = managerConfig[key]
  }
  return config
}

/** profile 骨架兜底（Python 侧 ensure_workspace 已写；桥直连测试也要能跑）。 */
function ensureProfileFiles () {
  try {
    mkdirSync(join(RUNTIME_ROOT, '.agentos', 'install-anchor'), { recursive: true })
    mkdirSync(join(RUNTIME_ROOT, '.dsh-home'), { recursive: true })
    const manifest = join(RUNTIME_ROOT, 'package.json')
    if (!existsSync(manifest)) {
      writeFileSync(manifest, JSON.stringify({
        name: 'agentos-dsh-profile',
        private: true,
        dependencies: {},
        dsh: { profile: { bundles: [] } },
      }, null, 2) + '\n')
    }
    for (const file of ['cordis.yml', 'cordis.patch.yml']) {
      const path = join(RUNTIME_ROOT, file)
      if (!existsSync(path)) writeFileSync(path, '[]\n')
    }
    const pnpmWorkspace = join(RUNTIME_ROOT, 'pnpm-workspace.yaml')
    if (!existsSync(pnpmWorkspace)) {
      writeFileSync(pnpmWorkspace, [
        'packages:', '  - .', '', 'nodeLinker: hoisted',
        'autoInstallPeers: false', 'registry: https://registry.npmmirror.com', '',
      ].join('\n'))
    }
    const anchor = join(RUNTIME_ROOT, '.agentos', 'install-anchor', 'package.json')
    if (!existsSync(anchor)) {
      writeFileSync(anchor, JSON.stringify({
        name: 'agentos-dsh-install-anchor', private: true, dependencies: {},
      }, null, 2) + '\n')
    }
  } catch (err) {
    managerError = `profile scaffold failed: ${messageOf(err)}`
    process.stderr.write(`[dsh-bridge] ${managerError}\n`)
  }
}

/** 从 profile 的 bundle patch 组合官方 rows 并装载（composition 失败回退扫描）。 */
async function loadProfilePackages (plan) {
  const fallback = Array.isArray(plan?.fallback?.packages) ? plan.fallback.packages : []
  if (ctx === null) {
    process.stderr.write('[dsh-bridge] profile rows skipped (tool chain not booted)\n')
    return
  }
  if (profileContext === null) {
    await loadPlanPackages(fallback)
    return
  }
  let rows = []
  try {
    const appBoot = await import(wsResolve('@deepseek-ai/dsh-app-boot'))
    const layers = appBoot.readProfilePatches('dsh', profileContext)
    rows = appBoot.composeEntries(layers, (message) => {
      process.stderr.write(`[dsh-bridge] profile patch: ${message}\n`)
    })
  } catch (err) {
    process.stderr.write(`[dsh-bridge] profile composition failed, fallback scan: ${messageOf(err)}\n`)
    await loadPlanPackages(fallback)
    return
  }
  let loaded = 0
  for (const row of rows) {
    if (row === null || typeof row !== 'object') continue
    if (row.disabled === true || row.group === true) continue
    const name = typeof row.name === 'string' ? row.name : ''
    if (name === '') continue
    if (BASE_CHAIN_MODULES.has(name)) {
      process.stderr.write(`[dsh-bridge] bundle row ${row.id ?? '?'} targets base chain module ${name}, skipped\n`)
      continue
    }
    try {
      const mod = await import(resolveProfileModule(name))
      const plugin = mod?.default ?? mod
      const fiber = await ctx.plugin(plugin, row.config === undefined ? {} : row.config)
      loaded += 1
      const info = fiberStateOf(fiber)
      bundleStates.push({ id: String(row.id ?? ''), name, ...info })
      process.stderr.write(`[dsh-bridge] bundle row loaded: ${row.id ?? '?'} (${name}) state=${info.label}\n`)
    } catch (err) {
      bundleStates.push({
        id: String(row.id ?? ''), name, state: null, label: 'failed', injects: [],
        error: messageOf(err),
      })
      process.stderr.write(`[dsh-bridge] bundle row ${row.id ?? '?'} (${name}) load failed: ${messageOf(err)}\n`)
    }
  }
  if (loaded === 0 && fallback.length > 0) {
    process.stderr.write('[dsh-bridge] profile composition yielded no loadable rows, fallback scan\n')
    await loadPlanPackages(fallback)
  }
}

function resolveProfileModule (name) {
  if (name.startsWith('file:')) return name
  try {
    return wsResolve(name)
  } catch (err) {
    if (name.startsWith('.') || name.startsWith('/') || /^[A-Za-z]:[\\/]/.test(name)) {
      return pathToFileURL(resolve(RUNTIME_ROOT, name)).href
    }
    throw err
  }
}

function messageOf (err) {
  return err instanceof Error ? `${err.name}: ${err.message}` : String(err)
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
  if (ctx === null) return
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

/** DSH parameters DSL（{field: {type, required?, description?}}）→ JSON Schema。
 *  已是 JSON Schema 形（部分社区插件直接写 schema，不走 DSL）→ 原样透传。 */
function paramsToJsonSchema (parameters) {
  if (parameters === undefined || parameters === null) return { type: 'object', properties: {} }
  if (parameters.type === 'object' && parameters.properties !== null
      && typeof parameters.properties === 'object') {
    return parameters
  }
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
  if (ctx === null) return out
  let entries = []
  try {
    // 动态枚举：boot 插件（read/glob/grep）+ 计划装载的工具包全部可见
    entries = [...(ctx.tools.view().visible.entries())]
  } catch {
    entries = ctx.tools === undefined
      ? []
      : ['read', 'glob', 'grep'].map((n) => [n, ctx.tools.get(n)]).filter(([, t]) => t !== undefined)
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
/**
 * 灵汐会话工作区 → DSH 会话对象（工具 exec.agent.session 的本地翻译）。
 *
 * - ``header.cwd`` = boot cwd（灵汐侧工作区）；``requestHeader().config`` =
 *   当前模型路由（llm.yaml 翻译 + provider 兜底）——read_image 等工具据此
 *   判图像能力；
 * - ``append`` 写内存事件日志并广播 ``session/event``，已挂的
 *   sessionProjections（todo 等）照常折叠（会话投影面等价）。
 */
function makeSession (sessionId) {
  const events = []
  let seq = 0
  const session = {
    header: { cwd: bootCwd, sessionId },
    inheritedEventCount: 0,
    get seq () { return seq },
    events,
    snapshotEvents (from, to) {
      const start = Number(from) || 0
      const end = to === undefined || to === null ? events.length : Number(to)
      return events.slice(start, end)
    },
    requestHeader () {
      return activeModel === null ? undefined : {
        config: { provider: activeModel.provider, model: activeModel.model },
      }
    },
    append (type, data) {
      const event = { type, data, seq }
      events.push(event)
      seq += 1
      if (ctx !== null) {
        try { ctx.emit('session/event', session, event) } catch (err) {
          process.stderr.write(`[dsh-bridge] session/event emit failed: ${messageOf(err)}
`)
        }
      }
      return event
    },
  }
  return session
}

function makeExec (callId, name, args, signal) {
  const session = makeSession(callId)
  return {
    callId,
    rootCallId: callId,
    name,
    arguments: args,
    signal,
    agent: {
      session,
      // 工具侧服务（working-directory 等）会取 agent.ctx.fiber.assertActive()：
      // 用桥的链 context 背书（同一 active fiber）。
      ctx,
      options: activeModel === null
        ? {}
        : { provider: activeModel.provider, model: activeModel.model },
    },
    deferContext() {},
    concludeTurn() {},
  }
}

async function callTool (id, params) {
  const name = String(params?.name ?? '')
  const args = params?.args ?? {}
  const timeoutMs = Number.isFinite(params?.timeoutMs) ? params.timeoutMs : 120_000
  const tool = booted && ctx?.tools !== undefined ? ctx.tools.get(name) : undefined
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
    process.stderr.write(`[dsh-bridge] tool ${name} failed: ${err instanceof Error ? (err.stack ?? err.message) : String(err)}
`)
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

/** 拦截点事件送回 DSH 监听器（垫片二/三：dispatchHook 按 DSH 真实签名展开
 *  tools/* 三参 + thisArg 代理 + 身份物化；词表外事件保持旧形态 waterfall）。
 *  服务面（垫片四）在 bootChain 经 ctx.provide('agentos', …) 挂载。 */
async function emitHook (id, params) {
  const event = String(params?.event ?? '')
  if (!event) {
    return { jsonrpc: '2.0', id, result: { ok: false, event, decision: null, error: 'missing event' } }
  }
  if (!booted || ctx === null) {
    return { jsonrpc: '2.0', id, result: { ok: false, event, decision: null, error: 'bridge not initialized' } }
  }
  try {
    const decision = await dispatchHook(ctx, identities, {
      event,
      payload: params?.payload ?? {},
      exec: params?.exec,
      result: params?.result,
      agentKey: typeof params?.agentKey === 'string' ? params.agentKey : null,
    })
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

/** 官方 plugin-manager 管理面（安装/卸载/启停/发现）；结果原样回传。 */
async function callManager (id, method, params) {
  const envelope = (ok, data, error) => ({
    jsonrpc: '2.0',
    id,
    result: { ok, data: data ?? null, error: error ?? null },
  })
  if (pluginManager === null) {
    return envelope(false, null, managerError ?? 'plugin manager unavailable')
  }
  try {
    switch (method) {
      case 'plugin/manager/list': {
        const bundles = await pluginManager.listBundles()
        let plugins = []
        try { plugins = await pluginManager.listPlugins() } catch (err) {
          process.stderr.write(`[dsh-bridge] listPlugins failed (management-only): ${messageOf(err)}\n`)
        }
        let registries = null
        try { registries = await pluginManager.registries() } catch { /* 发现面可选 */ }
        return envelope(true, { bundles, plugins, registries })
      }
      case 'plugin/manager/inspect':
        return envelope(true, await pluginManager.inspect(String(params?.spec ?? '')))
      case 'plugin/manager/install':
        return envelope(true, await pluginManager.installBundle(
          String(params?.spec ?? ''), params?.options ?? {}))
      case 'plugin/manager/cancel':
        return envelope(true, await pluginManager.cancelInstall(String(params?.requestId ?? '')))
      case 'plugin/manager/remove':
        return envelope(true, await pluginManager.removeBundle(String(params?.name ?? '')))
      case 'plugin/manager/enable':
        return envelope(true, await pluginManager.setBundleEnabled(String(params?.name ?? ''), true))
      case 'plugin/manager/disable':
        return envelope(true, await pluginManager.setBundleEnabled(String(params?.name ?? ''), false))
      case 'plugin/manager/registries':
        return envelope(true, await pluginManager.registries())
      default:
        return envelope(false, null, `unknown manager method: ${method}`)
    }
  } catch (err) {
    return envelope(false, null, messageOf(err))
  }
}

/** 宿主依赖补装：复用官方 `runProfilePnpm`（最小安装链路，非 bundle 安装）。
 *
 * 与 `installBundle` 的差别：不写 `dsh.profile.bundles`、不做 bundle 兼容性
 * 激活（`activateNewBundles: false`）；环境擦洗/输出落盘/空闲超时/取消都由
 * 官方实现负责。调用方（Python）保证与 installBundle 串行，无并发写 profile。
 */
async function callHostDep (id, params) {
  const envelope = (ok, data, error) => ({
    jsonrpc: '2.0',
    id,
    result: { ok, data: data ?? null, error: error ?? null },
  })
  if (profileContext === null) return envelope(false, null, 'profile context unavailable (bootstrap required)')
  const specs = Array.isArray(params?.specs)
    ? params.specs.filter((s) => typeof s === 'string' && s !== '')
    : []
  if (specs.length === 0) return envelope(true, { skipped: true, exitCode: 0 })
  let operations
  try {
    operations = await import(wsResolve('@deepseek-ai/dsh-plugin-manager/operations'))
  } catch (err) {
    return envelope(false, null, `official operations unavailable: ${messageOf(err)}`)
  }
  const packageManager = profileContext.packageManager ?? {}
  const registry = typeof managerConfigGlobal?.registry === 'string' && managerConfigGlobal.registry !== ''
    ? managerConfigGlobal.registry
    : null
  const args = ['add', ...specs, '--save-exact']
  if (registry !== null) args.push(`--registry=${registry}`)
  const idleTimeoutMs = Number(managerConfigGlobal?.idleTimeoutMs ?? 900000)
  const operationContext = {
    profile: profileContext.dir,
    dir: profileContext.dir,
    installAnchor: profileContext.installAnchor,
    cwd: profileContext.cwd,
    ...(profileContext.home === undefined ? {} : { home: profileContext.home }),
  }
  try {
    const result = await operations.runProfilePnpm(operationContext, args, {
      ...(packageManager.command === undefined ? {} : { command: packageManager.command }),
      ...(Array.isArray(packageManager.args) ? { args: packageManager.args } : {}),
      execution: 'service',
      outputBytes: Number(managerConfigGlobal?.outputBytes ?? 16384),
      activateNewBundles: false,
      // 只设官方「静默超时」（idleTimeoutMs 是输出静默界，不是总时长界）；
      // 不再挂 AbortSignal.timeout——绝对超时会砍掉仍在下载的长安装。
      idleTimeoutMs,
    })
    const ok = result?.exitCode === 0 && result?.timedOut !== true
    return envelope(ok, result, ok ? null : `host dep install failed (exit ${result?.exitCode ?? 'null'})`)
  } catch (err) {
    return envelope(false, null, messageOf(err))
  }
}

async function handleRequest (method, params) {
  switch (method) {
    case 'initialize': {
      const cwd = typeof params?.cwd === 'string' && params.cwd !== '' ? params.cwd : process.cwd()
      await boot(cwd, params?.bootPlan, params?.manager)
      return {
        serverInfo: {
          name: 'agentos-dsh-bridge',
          version: '0.2.1',
          webServerOrigin,
          webServerError,
          managerReady: pluginManager !== null,
          managerError,
          profile: profileContext !== null,
          bundleStates,
          providerStates,
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
      if (managerCtx !== null) {
        try { await managerCtx.dispose() } catch { /* 退出路径上的清理失败不阻塞 */ }
      }
      if (booted && ctx !== null) {
        try { await ctx.dispose() } catch { /* 退出路径上的清理失败不阻塞 */ }
      }
      return {}
    }
    default:
      throw new Error(`unknown method: ${method}`)
  }
}

// 一次性 manager 模式（`--manager-once <request.json>`）：不 boot 工具链与
// profile bundle，只挂官方 plugin-manager 跑一次变更后退出。Python 侧在装/卸
// 前先停常驻桥再调本模式——Windows 上 pnpm 要重命名 node_modules/<pkg>，
// 常驻桥持有包内文件句柄会 ERR_PNPM_EPERM（dev 实测 2026-11-09）。
const onceIdx = process.argv.indexOf('--manager-once')
if (onceIdx !== -1) {
  const requestPath = process.argv[onceIdx + 1]
  const request = JSON.parse(readFileSync(requestPath, 'utf8'))
  const cwd = typeof request?.cwd === 'string' && request.cwd !== '' ? request.cwd : process.cwd()
  profileContext = buildProfileContext(cwd, request?.manager)
  await bootManager(request?.manager)
  const result = await callManager(1, String(request?.method ?? ''), request?.params ?? {})
  process.stdout.write(`${JSON.stringify(result.result)}\n`)
  process.exit(0)
}

/** 退出路径统一清理：与 ``shutdown`` 帧同构，但有界等待——宿主进程已死时
    没有任何人在等响应，清理不得把退出拖成孤儿（父死子退契约）。 */
let exiting = false
async function disposeAndExit(code = 0) {
  if (exiting) return
  exiting = true
  const cleanup = (async () => {
    if (managerCtx !== null) {
      try { await managerCtx.dispose() } catch { /* 退出路径上的清理失败不阻塞 */ }
    }
    if (booted && ctx !== null) {
      try { await ctx.dispose() } catch { /* 退出路径上的清理失败不阻塞 */ }
    }
  })()
  await Promise.race([cleanup, new Promise((resolve) => setTimeout(resolve, 2000).unref())])
  process.exit(code)
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
  // Node→Python 反向 RPC 的响应帧（垫片四）：上行请求 id 为 "up-N" 字符串，
  // 命中挂起表即唤醒等待者；不进下行方法分发（防误当未知方法报错）。
  if (typeof msg?.id === 'string' && msg.id.startsWith('up-')) {
    const settle = pendingUpstream.get(msg.id)
    if (settle !== undefined) {
      settle(msg.result ?? { ok: false, error: msg.error?.message ?? 'upstream error frame' })
    }
    return
  }
  void (async () => {
    let response
    if (msg.method === 'tool/call') {
      response = await callTool(msg.id, msg.params)
    } else if (msg.method === 'service/call') {
      response = await callService(msg.id, msg.params)
    } else if (msg.method === 'hook/emit') {
      response = await emitHook(msg.id, msg.params)
    } else if (msg.method === 'http/request') {
      response = await httpRequest(msg.id, msg.params)
    } else if (msg.method === 'plugin/host-dep/add') {
      response = await callHostDep(msg.id, msg.params)
    } else if (typeof msg.method === 'string' && msg.method.startsWith('plugin/manager/')) {
      response = await callManager(msg.id, msg.method, msg.params)
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

// stdin 关闭 = 宿主进程死亡（sidecar 被强杀/重载时 on_unload 不执行，
// shutdown 帧永远不会到达）。没有这个处理器，已 boot 的 DSH context 持有的
// 定时器/句柄会让本进程永久常驻——2026-11-12 实测 dev 内核 respawn 后遗留
// 5 个孤儿桥（每个 ~110MB）。
rl.on('close', () => {
  process.stderr.write('[dsh-bridge] stdin closed (parent gone), disposing and exiting\n')
  void disposeAndExit(0)
})

process.stderr.write(`[dsh-bridge] ready (workspace=${RUNTIME_ROOT || 'not-configured'})\n`)
