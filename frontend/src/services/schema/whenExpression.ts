/**
 * when 条件表达式求值器（ADR §3.4）
 *
 * VS Code when 子句的精简实现：让 contributes 贡献点根据 context keys 动态显隐。
 *
 * 支持的 context keys 基础集（ADR §3.4）：
 * - pipeline.running / pipeline.idle        流水线运行/空闲
 * - workspace.focus / chat.focus            工作区/聊天聚焦
 * - resource.isFile / resource.extname      当前资源是文件/扩展名
 * - interaction.pending                     有待处理交互
 * - 插件可贡献自定义 context key（由各事件更新 ContextKeysStore）
 *
 * 文法（优先级从低到高）：
 *   orExpr     := andExpr ( '||' andExpr )*
 *   andExpr    := eqExpr ( '&&' eqExpr )*
 *   eqExpr     := unary ( ('==' | '!=') unary )?
 *   unary      := '!' unary | primary
 *   primary    := '(' orExpr ')' | contextKey | literal
 *
 * 求值规则：
 * - 单独的 contextKey：按 truthiness（布尔直判；非空字符串为真；空字符串/undefined 为假）
 * - contextKey == literal：值严格相等（字符串去引号比较，布尔字面量转布尔）
 * - 非法/无法解析的表达式统一返回 false（fail-closed：失配隐藏）
 */

/** Context keys 状态：键 → 值（布尔或字符串） */
export type ContextKeys = Record<string, boolean | string>

/** 词法 token 类型 */
type TokenType = 'OR' | 'AND' | 'EQ' | 'NEQ' | 'NOT' | 'LPAREN' | 'RPAREN' | 'KEY' | 'LITERAL' | 'EOF'

interface Token {
  type: TokenType
  /** KEY：context key 名；LITERAL：去引号后的字面量 */
  value: string
}

/**
 * 词法分析：把 when 字符串切成 token 流
 *
 * 识别：|| && == != ! ( ) 裸标识符（context key，允许 a.b 点号） 单/双引号字符串字面量
 */
function tokenize(expr: string): Token[] | null {
  const tokens: Token[] = []
  let i = 0
  const n = expr.length
  while (i < n) {
    const ch = expr[i]
    // 跳过空白
    if (ch === ' ' || ch === '\t' || ch === '\n' || ch === '\r') {
      i++
      continue
    }
    if (ch === '|') {
      if (expr[i + 1] === '|') {
        tokens.push({ type: 'OR', value: '||' })
        i += 2
        continue
      }
      return null // 单 | 非法
    }
    if (ch === '&') {
      if (expr[i + 1] === '&') {
        tokens.push({ type: 'AND', value: '&&' })
        i += 2
        continue
      }
      return null
    }
    if (ch === '=') {
      if (expr[i + 1] === '=') {
        tokens.push({ type: 'EQ', value: '==' })
        i += 2
        continue
      }
      return null
    }
    if (ch === '!') {
      if (expr[i + 1] === '=') {
        tokens.push({ type: 'NEQ', value: '!=' })
        i += 2
        continue
      }
      tokens.push({ type: 'NOT', value: '!' })
      i++
      continue
    }
    if (ch === '(') {
      tokens.push({ type: 'LPAREN', value: '(' })
      i++
      continue
    }
    if (ch === ')') {
      tokens.push({ type: 'RPAREN', value: ')' })
      i++
      continue
    }
    // 字符串字面量
    if (ch === "'" || ch === '"') {
      const quote = ch
      i++
      let str = ''
      let closed = false
      while (i < n) {
        if (expr[i] === quote) {
          closed = true
          i++
          break
        }
        str += expr[i]
        i++
      }
      if (!closed) return null
      tokens.push({ type: 'LITERAL', value: str })
      continue
    }
    // 裸标识符 / context key（含点号）/ 布尔字面量 true|false / 数字
    if (isIdentStart(ch)) {
      let ident = ''
      while (i < n && isIdentPart(expr[i])) {
        ident += expr[i]
        i++
      }
      // true/false 归一为 LITERAL（保留原始串，求值时转布尔）
      if (ident === 'true' || ident === 'false') {
        tokens.push({ type: 'LITERAL', value: ident })
      } else {
        tokens.push({ type: 'KEY', value: ident })
      }
      continue
    }
    // 未知字符
    return null
  }
  tokens.push({ type: 'EOF', value: '' })
  return tokens
}

function isIdentStart(ch: string): boolean {
  return /[a-zA-Z_]/.test(ch)
}

function isIdentPart(ch: string): boolean {
  return /[a-zA-Z0-9_.]/.test(ch)
}

/** 递归下降解析器 + 求值器 */
class Parser {
  private pos = 0
  constructor(private readonly tokens: Token[], private readonly ctx: ContextKeys) {}

  private peek(): Token {
    return this.tokens[this.pos]
  }

  private next(): Token {
    return this.tokens[this.pos++]
  }

  /** 入口：解析整个表达式；失败抛错 */
  parse(): boolean {
    const v = this.parseOr()
    if (this.peek().type !== 'EOF') throw new Error('trailing tokens')
    return v
  }

  /** orExpr := andExpr ( '||' andExpr )* */
  private parseOr(): boolean {
    let left = this.parseAnd()
    while (this.peek().type === 'OR') {
      this.next()
      const right = this.parseAnd()
      left = left || right
    }
    return left
  }

  /** andExpr := eqExpr ( '&&' eqExpr )* */
  private parseAnd(): boolean {
    let left = this.parseEq()
    while (this.peek().type === 'AND') {
      this.next()
      const right = this.parseEq()
      left = left && right
    }
    return left
  }

  /** eqExpr := unary ( ('==' | '!=') unary )? */
  private parseEq(): boolean {
    const left = this.parseUnary()
    const op = this.peek()
    if (op.type === 'EQ' || op.type === 'NEQ') {
      this.next()
      const right = this.parseUnary()
      const equal = left.value === right.value
      return op.type === 'EQ' ? equal : !equal
    }
    // 无运算符：按 truthiness
    return left.truthy
  }

  /** unary := '!' unary | primary */
  private parseUnary(): { value: string; truthy: boolean } {
    if (this.peek().type === 'NOT') {
      this.next()
      const operand = this.parseUnary()
      return { value: String(!operand.truthy), truthy: !operand.truthy }
    }
    return this.parsePrimary()
  }

  /** primary := '(' orExpr ')' | KEY | LITERAL */
  private parsePrimary(): { value: string; truthy: boolean } {
    const tok = this.peek()
    if (tok.type === 'LPAREN') {
      this.next()
      const v = this.parseOr()
      if (this.peek().type !== 'RPAREN') throw new Error('unclosed paren')
      this.next()
      return { value: String(v), truthy: v }
    }
    if (tok.type === 'KEY') {
      this.next()
      const raw = this.ctx[tok.value]
      return { value: String(raw ?? ''), truthy: isTruthy(raw) }
    }
    if (tok.type === 'LITERAL') {
      this.next()
      // 布尔字面量
      if (tok.value === 'true') return { value: 'true', truthy: true }
      if (tok.value === 'false') return { value: 'false', truthy: false }
      return { value: tok.value, truthy: tok.value !== '' }
    }
    throw new Error(`unexpected token ${tok.type}`)
  }
}

/** context key 值的 truthiness */
function isTruthy(v: boolean | string | undefined): boolean {
  if (v === undefined || v === null) return false
  if (typeof v === 'boolean') return v
  return v !== ''
}

/**
 * 求值 when 表达式
 *
 * @param when - 条件表达式字符串（undefined/空 → 恒真）
 * @param ctx - 当前 context keys 状态
 * @returns 是否可见（非法表达式 fail-closed 返回 false）
 */
export function evaluateWhen(when: string | undefined, ctx: ContextKeys): boolean {
  if (when === undefined || when.trim() === '') return true
  const tokens = tokenize(when)
  if (tokens === null) return false
  try {
    return new Parser(tokens, ctx).parse()
  } catch {
    return false
  }
}
