/**
 * DSH vendor 组件出口（task_dsh_plugin_adapter 任务 3）。
 *
 * 统一从本模块导入，消费方（render 意图路由层）不直接触达文件级路径——
 * DSH 升级重移植时只改本目录内部。出处/版本锁定见 ./README.md。
 */
import './dsh-tokens.css'

export { DiffBlock, type DiffHunk, DEFAULT_DIFF_MAX_LINES } from './DiffBlock.tsx'
export { JsonTree, type JsonTreeProps } from './JsonTree.tsx'
export { ReadBlock, type ReadBlockLine, DEFAULT_READ_MAX_LINES } from './ReadBlock.tsx'
export { SearchBlock, type SearchBlockProps, type SearchFileGroup } from './SearchBlock.tsx'
export { TerminalBlock, type TerminalBlockProps } from './TerminalBlock.tsx'
export { WebBlock, type WebBlockProps, type WebSourceView } from './WebBlock.tsx'
export { CodeBlock, type CodeBlockProps } from './CodeBlock.tsx'
export { Pill } from './Pill.tsx'
export { StateDot, type StateDotState } from './StateDot.tsx'
