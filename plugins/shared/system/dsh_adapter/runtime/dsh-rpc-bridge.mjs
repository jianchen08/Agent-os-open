#!/usr/bin/env node
/**
 * AgentOS ↔ DSH 工具桥（task_dsh_plugin_adapter 任务 4 通道 A：runtime 改造桥接）。
 *
 * 这是 DSH SDK runtime 的「rpc-export fork」：不改动 DSH 仓库任何文件（只读
 * 铁律），而是以绝对路径导入其已构建产物（锁定 commit
 * 47f943859bef60e4160492346772ded9b24f765a / 0.1.0-rc.5），boot 一个最小
 * cordis context（systemPrompt + tools + fs-local + subprocess + tool-fs +
 * tool-fs-search），把注册进 tools 服务的 DSH 工具经 stdio JSON-RPC 暴露给
 * AgentOS 侧的 Python sidecar 宿主（bridge.py）。
 *
 * DSH 工具跑在它自己的 Node runtime（本进程），实现零翻译；本脚本只做
 * 「契约出口」——等价于在 DSH SDK server 的 handleRequest switch 里加
 * `tool/<name>` case，改动集中在这一个文件便于跟随 upstream rebase。
 *
 * 协议（newline-delimited JSON-RPC，stdout 专属协议帧，日志一律走 stderr）：
 *   initialize {cwd}            → {serverInfo, tools: [{name, description, input_schema, output_schema}]}
 *   tool/call {name, args, timeoutMs?} → {success, data, error, duration_ms}
 *   shutdown                    → {} （dispose 后 exit 0）
 *
 * MIT 出处：DSH 各包 MIT License, Copyright (c) 2026 DeepSeek；本文件是
 * AgentOS 侧新增的宿主代码（非 DSH 复制）。
 */

import { createInterface } from 'node:readline'

const DSH_REPO_ROOT = process.env.AGENTOS_DSH_REPO_ROOT ?? 'D:/reference_repos/deepseek-harness'
const CLI_MODULES = `${DSH_REPO_ROOT}/apps/cli/node_modules/@deepseek-ai`
const PKG = (name) => `file:///${DSH_REPO_ROOT}/apps/cli/node_modules/@deepseek-ai/${name}/lib/index.js`
// subprocess-local 不在 apps/cli 链接集里，走包真实路径
const SUBPROCESS_LOCAL = `file:///${DSH_REPO_ROOT}/packages/subprocess/subprocess-local/lib/index.js`

let ctx = null
let booted = false

async function boot(cwd) {
  const { Context } = await import(PKG('cordis'))
  const SystemPrompt = (await import(PKG('dsh-system-prompt'))).default
  const { ToolRuntime } = await import(PKG('dsh-tools'))
  const LocalFileSystem = (await import(PKG('dsh-fs-local'))).default
  const SubprocessLocal = (await import(SUBPROCESS_LOCAL)).default
  const ToolFs = await import(PKG('dsh-tool-fs'))
  const ToolFsSearch = await import(PKG('dsh-tool-fs-search'))

  ctx = new Context()
  await ctx.plugin(SystemPrompt)
  await ctx.plugin(ToolRuntime)
  await ctx.plugin(LocalFileSystem, { cwd })
  await ctx.plugin(SubprocessLocal)
  await ctx.plugin(ToolFs)
  await ctx.plugin(ToolFsSearch, { sampleOverCapGlobResults: true })
  booted = true
}

/** DSH parameters DSL（{field: {type, required?, description?}}）→ JSON Schema。 */
function paramsToJsonSchema(parameters) {
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
function listTools() {
  const out = []
  for (const name of ['read', 'glob', 'grep']) {
    const tool = ctx.tools.get(name)
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
function makeExec(callId, name, args, signal) {
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

async function callTool(id, params) {
  const name = String(params?.name ?? '')
  const args = params?.args ?? {}
  const timeoutMs = Number.isFinite(params?.timeoutMs) ? params.timeoutMs : 120_000
  const tool = booted ? ctx.tools.get(name) : undefined
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
        duration_ms: Math.round((performance.now() - start) * 10) / 10,
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
        duration_ms: Math.round((performance.now() - start) * 10) / 10,
      },
    }
  } finally {
    clearTimeout(timer)
  }
}

async function handleRequest(method, params) {
  switch (method) {
    case 'initialize': {
      const cwd = typeof params?.cwd === 'string' && params.cwd !== '' ? params.cwd : process.cwd()
      await boot(cwd)
      return { serverInfo: { name: 'agentos-dsh-bridge', version: '0.1.0' }, tools: listTools() }
    }
    case 'tools/list': {
      if (!booted) return { tools: [] }
      return { tools: listTools() }
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

process.stderr.write(`[dsh-bridge] ready (repo=${DSH_REPO_ROOT})\n`)
