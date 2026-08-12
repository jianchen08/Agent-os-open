/**
 * builtinToolChatCards —— 内置工具的 chat_card 声明（TC T1：应用基础设施到存量工具）
 *
 * 把原手写 registerToolCard({...}) 命令式配置翻译成等价的 ui.chat_card 声明式配置，
 * 由 chatCardInterpreter 翻译成 ActivityDetailBlock[]，ActivityCard 原样渲染。
 *
 * 装载方式：GrowthLoop.reloadContributionRegistry 在 loadChatCardDeclarations(schema)
 * （清空全表 + 装 schema 声明）之后调用 registerBuiltinToolChatCards()，以
 * addChatCardDeclaration 逐个追加——builtin 覆盖同名 schema 声明，且在 schema 热重载后依然生效。
 *
 * 迁移范围：file_read / bash_execute / web_search / fetch / task_submit 共 5 个。
 * 保留手写（未迁）：
 *  - file_write：依赖 buildDiffStat（从 result 计算 +X -Y 徽标 → activity.diffStat），
 *    声明层目前不产出 diffStat；保留手写以不丢失头部增删行徽标（见 gap 说明）。
 *  - human_interaction：仅设 runningColor，极简，按约定保留。
 *
 * 等价性说明（不逐字节相同，但块类型/字段/折叠语义一致）：
 *  - truncate 过滤器用单字符省略号 `…`，手写用 `...`；截断长度略有差异（可接受）。
 *  - 标题 default 过滤器在 source 缺失时兜底（如 bash 无 command → "执行命令"）。
 *  - file_read 经 filePathSource 保留「点击标题打开文件」（等价手写 hasFilePath）。
 */

import { addChatCardDeclaration } from './chatCardInterpreter'
import type { ChatCardDeclaration } from './chatCardInterpreter'

/**
 * file_read —— 读取文件
 *
 * 手写语义：title="读取 <basename>"; 详情=文件路径(code)+文件内容(code 折叠);
 * hasFilePath 使标题可点击打开文件。
 */
const fileReadDecl: ChatCardDeclaration = {
  icon: 'file_read',
  title: '读取 {{args.file_path | basename | default:file_read}}',
  // 等价手写 hasFilePath：求值非空 → enhance 注入 filePath + onOpenFile，标题可点击打开
  filePathSource: 'args.file_path',
  blocks: [
    { type: 'code', label: '文件路径', source: 'args.file_path', collapsible: false },
    { type: 'code', label: '文件内容', source: 'result', collapsible: true, defaultExpanded: false },
  ],
}

/**
 * bash_execute —— 执行命令
 *
 * 手写语义：title=命令首行(>60 截断)，无命令→"执行命令";
 * 详情=命令(code,bash,默认展开)+输出(code,text,折叠)+错误(text,默认展开)。
 *
 * 差异：first_line 过滤器不 trim（手写 trim，命令极少有首行空白，可忽略）；
 * result 为纯文本输出，靠 interpreter 的 result 解析失败回退原始字符串取值。
 */
const bashExecuteDecl: ChatCardDeclaration = {
  icon: 'terminal',
  title: '{{args.command | first_line | truncate:60 | default:执行命令}}',
  blocks: [
    {
      type: 'code',
      label: '命令',
      source: 'args.command',
      language: 'bash',
      collapsible: true,
      defaultExpanded: true,
    },
    {
      type: 'code',
      label: '输出',
      source: 'result',
      language: 'text',
      collapsible: true,
      defaultExpanded: false,
    },
    { type: 'text', label: '错误', source: 'error', collapsible: true, defaultExpanded: true },
  ],
}

/**
 * web_search —— 网页搜索
 *
 * 手写语义：title=query(>50 截断)，无 query→"网页搜索";
 * 详情=搜索内容(text)+搜索结果(text 折叠)。
 */
const webSearchDecl: ChatCardDeclaration = {
  icon: 'globe',
  title: '{{args.query | truncate:50 | default:网页搜索}}',
  blocks: [
    { type: 'text', label: '搜索内容', source: 'args.query', collapsible: false },
    { type: 'text', label: '搜索结果', source: 'result', collapsible: true, defaultExpanded: false },
  ],
}

/**
 * fetch —— 访问网页
 *
 * 手写语义：title="访问 <hostname>"（url 无协议时手写补 https://），无 url→"访问网页";
 * 详情=URL(code)+页面内容(text 折叠，>500 截断)。
 *
 * 差异：hostname 过滤器对「无协议且含路径」的 url 会回退原串（手写能取 hostname），
 * 实际 fetch 的 url 均带协议，影响可忽略；截断后缀为 `…`（手写 `... (内容已截断)`）。
 */
const fetchDecl: ChatCardDeclaration = {
  icon: 'link',
  title: '访问 {{args.url | hostname | default:网页}}',
  blocks: [
    { type: 'code', label: 'URL', source: 'args.url', collapsible: false },
    {
      type: 'text',
      label: '页面内容',
      source: 'result | truncate:500',
      collapsible: true,
      defaultExpanded: false,
    },
  ],
}

/**
 * task_submit —— 提交任务
 *
 * 手写语义：title="提交任务: <goal_title>"（多级回退 goal_title→goal.title→description→...，
 * 此处用 goal_title + default 覆盖当前 schema 平铺字段；legacy 嵌套回退为边界场景）;
 * 详情=任务目标(text)+详细描述(text 折叠)+执行者(text)+提交结果。
 *
 * 提交结果块：手写把 task_id/status/title/message 聚合成一个 text 块；声明层 text 块只取单 source，
 * 故用 kv 块按字段呈现同一组信息（块类型 kv vs text，字段/折叠语义等价）。
 */
const taskSubmitDecl: ChatCardDeclaration = {
  icon: 'target',
  title: '提交任务: {{args.goal_title | default:任务提交}}',
  blocks: [
    { type: 'text', label: '任务目标', source: 'args.goal_title', collapsible: false },
    {
      type: 'text',
      label: '详细描述',
      source: 'args.goal_description',
      collapsible: true,
      defaultExpanded: false,
    },
    { type: 'text', label: '执行者', source: 'args.target_id', collapsible: false },
    {
      type: 'kv',
      label: '提交结果',
      collapsible: true,
      defaultExpanded: false,
      fields: [
        { key: '任务ID', source: 'output.task_id' },
        { key: '状态', source: 'output.status' },
        { key: '标题', source: 'output.title' },
        { key: '消息', source: 'output.message' },
      ],
    },
  ],
}

/** 内置工具 chat_card 声明表（name → 声明） */
export const BUILTIN_TOOL_CHAT_CARDS: Array<{ name: string; ui: { chat_card: ChatCardDeclaration } }> =
  [
    { name: 'file_read', ui: { chat_card: fileReadDecl } },
    { name: 'bash_execute', ui: { chat_card: bashExecuteDecl } },
    { name: 'web_search', ui: { chat_card: webSearchDecl } },
    { name: 'fetch', ui: { chat_card: fetchDecl } },
    { name: 'task_submit', ui: { chat_card: taskSubmitDecl } },
  ]

/**
 * 注册内置工具的 chat_card 声明（追加，不清空注册表）。
 *
 * 在 GrowthLoop.reloadContributionRegistry 中、loadChatCardDeclarations(schema) 之后调用：
 * schema 声明先装入（清空全表），builtin 再追加覆盖，从而 builtin 在 schema 热重载后依然生效。
 */
export function registerBuiltinToolChatCards(): void {
  for (const t of BUILTIN_TOOL_CHAT_CARDS) {
    addChatCardDeclaration(t.name, t.ui.chat_card)
  }
}
