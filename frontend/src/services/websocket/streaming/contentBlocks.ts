/**
 * contentBlocks 操作辅助函数
 */

/**
 * 追加文本内容到 contentBlocks
 */
export function appendTextBlock(prevBlocks: any[], content: string, messageId: string): any[] {
  const blocks = prevBlocks ? [...prevBlocks] : []
  const lastBlock = blocks[blocks.length - 1]
  if (lastBlock?.type === 'text') {
    blocks[blocks.length - 1] = { ...lastBlock, text: (lastBlock.text || '') + content }
  } else {
    blocks.push({ type: 'text', text: content, sourceId: messageId })
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
