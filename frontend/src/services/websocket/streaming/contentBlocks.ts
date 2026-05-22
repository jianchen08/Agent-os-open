/**
 * contentBlocks 操作辅助函数
 */

/**
 * 追加文本内容到 contentBlocks
 *
 * sequence 参数用于排序：文本块和工具块共享统一的全局递增序号，
 * 确保 buildFragments 能按后端实际输出顺序交替渲染文本和工具卡片。
 * 合并到已有文本块时，保留原有 sequence；新建文本块时使用传入的 sequence。
 */
export function appendTextBlock(
  prevBlocks: any[], content: string, messageId: string, sequence?: number,
): any[] {
  const blocks = prevBlocks ? [...prevBlocks] : []
  const lastBlock = blocks[blocks.length - 1]
  if (lastBlock?.type === 'text') {
    const merged = { ...lastBlock, text: (lastBlock.text || '') + content }
    if (sequence !== undefined && merged.sequence === undefined) {
      merged.sequence = sequence
    }
    blocks[blocks.length - 1] = merged
  } else {
    const newBlock: any = { type: 'text', text: content, sourceId: messageId }
    if (sequence !== undefined) newBlock.sequence = sequence
    blocks.push(newBlock)
  }
  return blocks
}

/**
 * 追加思考内容到 contentBlocks
 */
export function appendThinkingChunk(prevBlocks: any[], chunk: string): any[] {
  const blocks = prevBlocks ? [...prevBlocks] : []
  const lastIdx = blocks.findLastIndex((b) => b.type === 'thinking' && b.thinking?.isThinking)
  if (lastIdx !== -1) {
    const block = { ...blocks[lastIdx] }
    block.thinking = { content: (block.thinking?.content || '') + chunk, isThinking: true }
    blocks[lastIdx] = block
  }
  return blocks
}

/**
 * 结束思考块，将 isThinking 设为 false
 */
export function endThinkingBlock(prevBlocks: any[]): any[] {
  const blocks = prevBlocks ? [...prevBlocks] : []
  const lastIdx = blocks.findLastIndex((b) => b.type === 'thinking' && b.thinking?.isThinking)
  if (lastIdx !== -1) {
    const block = { ...blocks[lastIdx] }
    block.thinking = { content: block.thinking?.content || '', isThinking: false }
    blocks[lastIdx] = block
  }
  return blocks
}
