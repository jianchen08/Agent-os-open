/**
 * 本仓自定义 ESLint 规则（OBS-R258-1 CI 固化）
 *
 * 规则一 no-swallowed-errors —— 查「吞错误」（全 src 生效）：
 *   - 空 catch 块（catch {} / catch { /* 注释 *\/ }）；
 *   - catch 块只 console 不 rethrow/不上报；
 *   - .catch(console.*)（同上，promise 链形态）；
 *   - .catch(() => 兜底值) 静默兜底：处理函数既不 throw / Promise.reject、
 *     也不引用任何外部标识符（setError/toast/reportError/日志器等任一外部
 *     引用即视为「有处理通道」，保守放行以压误报）。
 *
 *   豁免口径 = 「显式注释」：catch 块/处理函数体内已有说明性注释（记载为何
 *   此处有意降级）即视为登记放行；裸吞（无任何注释）报错，修复或补
 *   `// HACK: <原因>` 登记。TODO/FIXME 注释不算豁免（那是欠账不是决策）。
 *
 * 规则二 no-bare-usequery-in-pages —— 查「空/失败未区分」（pages 目录生效）：
 *   pages 目录禁裸用 useQuery（四态约定：页面数据面一律走 useAsyncResource
 *   或 hooks/queries/* 标准入口，loading/error/empty/ready 穷举渲染）。
 *   未迁移页面调用点逐行 `// eslint-disable-next-line local-rules/no-bare-usequery-in-pages`
 *   + HACK 原因登记（棘轮：新增调用点仍会被拦）。
 */

const PURE_GLOBALS = new Set([
  'Error', 'String', 'Number', 'Boolean', 'JSON', 'Math', 'Date',
  'Object', 'Array', 'Promise', 'undefined', 'NaN', 'Infinity',
  'globalThis', 'window', 'console',
])

/** 递归走子树（跳过 parent/loc/range 等元属性） */
function walk(node, visit) {
  if (!node || typeof node.type !== 'string') return
  visit(node)
  for (const key of Object.keys(node)) {
    if (key === 'parent' || key === 'loc' || key === 'range') continue
    const value = node[key]
    if (Array.isArray(value)) {
      for (const child of value) walk(child, visit)
    } else if (value && typeof value.type === 'string') {
      walk(value, visit)
    }
  }
}

/** 收集函数处理器的参数绑定名（Identifier/解构模式展开） */
function collectParamNames(param, names) {
  walk(param, (n) => {
    if (n.type !== 'Identifier') return
    const parent = n.parent
    const isPropertyKey =
      parent?.type === 'MemberExpression' && !parent.computed && parent.property === n
    if (!isPropertyKey) names.add(n.name)
  })
}

/** 块内是否登记了说明性注释（豁免口径）；TODO/FIXME 是欠账不是决策，不算 */
function hasRationaleComment(sourceCode, node) {
  return sourceCode
    .getCommentsInside(node)
    .some((comment) => !/^\s*(TODO|FIXME)\b/i.test(comment.value))
}

/** handler 是否仅引用纯工具全局/console（即无任何外部处理通道） */
function analyzeHandlerBody(fn) {
  const paramNames = new Set()
  for (const param of fn.params) collectParamNames(param, paramNames)

  let hasThrow = false
  let hasPromiseReject = false
  const refs = new Set()

  walk(fn.body, (n) => {
    if (n.type === 'ThrowStatement') hasThrow = true
    if (
      n.type === 'MemberExpression' &&
      !n.computed &&
      n.object?.type === 'Identifier' &&
      n.object.name === 'Promise' &&
      n.property?.type === 'Identifier' &&
      n.property.name === 'reject'
    ) {
      hasPromiseReject = true
    }
    if (n.type !== 'Identifier') return
    const parent = n.parent
    const isPropertyKey =
      (parent?.type === 'MemberExpression' && !parent.computed && parent.property === n) ||
      (parent?.type === 'Property' && !parent.computed && parent.key === n && !parent.shorthand)
    if (isPropertyKey) return
    if (!paramNames.has(n.name)) refs.add(n.name)
  })

  return { hasThrow, hasPromiseReject, refs }
}

const noSwallowedErrors = {
  meta: {
    type: 'problem',
    docs: {
      description:
        '禁止吞错误：空 catch / 只 console 的 catch / .catch(() => 兜底值) 静默降级（块内显式注释豁免；TODO/FIXME 不算）',
    },
    schema: [],
    messages: {
      emptyCatch:
        '空 catch 块 = 吞错误（OBS-R258-1）。要么处理要么传播；确属有意忽略须在块内登记说明注释（或 `// HACK: <原因>`）。',
      consoleOnlyCatch:
        'catch 块只 console 不 rethrow/不上报 = 吞错误一笔带过（OBS-R258-1）。改为向上传播、接入错误上报，或在块内登记说明注释（为何记日志即止）。',
      consoleOnlyPromiseCatch:
        '.catch(console.*) 只记日志不传播（OBS-R258-1）。改为向上传播、接入错误上报，或改块状处理函数并登记说明注释。',
      silentFallbackCatch:
        '.catch(() => 兜底值) 静默降级 = 失败伪装成正常数据（OBS-R258-1）。须 throw 向上传播、接入错误上报/UI 态，或改块状处理函数并在块内登记说明注释。',
    },
  },
  create(context) {
    const sourceCode = context.sourceCode

    return {
      CatchClause(node) {
        if (hasRationaleComment(sourceCode, node.body)) return
        const body = node.body.body
        if (body.length === 0) {
          context.report({ node, messageId: 'emptyCatch' })
          return
        }
        // 只 console：每条语句都是 console.* 调用表达式，且无其他外部引用
        const consoleOnly =
          body.every(
            (statement) =>
              statement.type === 'ExpressionStatement' &&
              statement.expression?.type === 'CallExpression' &&
              statement.expression.callee?.type === 'MemberExpression' &&
              statement.expression.callee.object?.type === 'Identifier' &&
              statement.expression.callee.object.name === 'console',
          ) &&
          (() => {
            const paramNames = new Set()
            if (node.param) collectParamNames(node.param, paramNames)
            const refs = new Set()
            walk(node.body, (n) => {
              if (n.type !== 'Identifier') return
              const parent = n.parent
              const isPropertyKey =
                (parent?.type === 'MemberExpression' && !parent.computed && parent.property === n) ||
                (parent?.type === 'Property' && !parent.computed && parent.key === n && !parent.shorthand)
              if (isPropertyKey || paramNames.has(n.name)) return
              refs.add(n.name)
            })
            for (const name of refs) {
              if (name !== 'console') return false
            }
            return true
          })()
        if (consoleOnly) {
          context.report({ node, messageId: 'consoleOnlyCatch' })
        }
      },

      // .catch(handler)：promise 链形态的吞错误
      'CallExpression[callee.type="MemberExpression"][callee.property.name="catch"]'(node) {
        const handler = node.arguments[0]
        if (!handler) return

        if (handler.type === 'MemberExpression') {
          if (
            handler.object?.type === 'Identifier' &&
            handler.object.name === 'console'
          ) {
            context.report({ node: handler, messageId: 'consoleOnlyPromiseCatch' })
          }
          return
        }
        if (handler.type !== 'ArrowFunctionExpression' && handler.type !== 'FunctionExpression') {
          return
        }
        // 块体先看注释豁免；表达式体装不下注释，直接走通道分析
        if (handler.body.type === 'BlockStatement' && hasRationaleComment(sourceCode, handler.body)) {
          return
        }

        const { hasThrow, hasPromiseReject, refs } = analyzeHandlerBody(handler)
        if (hasThrow || hasPromiseReject) return

        // 有任一外部引用（setState/toast/reportError/日志器等）即视为有处理通道
        for (const name of refs) {
          if (name !== 'console' && !PURE_GLOBALS.has(name)) return
        }
        // 无引用（字面量兜底/空处理）或仅 console + 纯工具全局 → 吞错误
        context.report({ node: handler, messageId: 'silentFallbackCatch' })
      },
    }
  },
}

const noBareUseQueryInPages = {
  meta: {
    type: 'problem',
    docs: {
      description:
        'pages 目录禁裸用 useQuery：页面数据面走 useAsyncResource / hooks/queries/* 四态标准入口',
    },
    schema: [],
    messages: {
      bareUseQuery:
        'pages 目录禁裸用 useQuery（OBS-R258-1 四态约定）：改用 useAsyncResource/hooks/queries/* 标准入口，页面按 status 穷举渲染 loading/error/empty/ready；未迁移页调用点逐行 eslint-disable + HACK 原因登记。',
    },
  },
  create(context) {
    return {
      CallExpression(node) {
        if (node.callee?.type === 'Identifier' && node.callee.name === 'useQuery') {
          context.report({ node, messageId: 'bareUseQuery' })
        }
      },
    }
  },
}

export default {
  rules: {
    'no-swallowed-errors': noSwallowedErrors,
    'no-bare-usequery-in-pages': noBareUseQueryInPages,
  },
}
