/**
 * 流式 handler 测试家族共享的事件工厂。
 *
 * makeEvent 曾在 10+ 个测试文件中逐字复制（jscpd 克隆门禁重复源）；
 * 统一为参数化工厂：`const makeEvent = makeEventFactory(PIPELINE_ID, MESSAGE_ID)`。
 */
export function makeEventFactory(pipelineId: string, messageId: string) {
  return (eventType: string, data: Record<string, any>) => ({
    type: eventType,
    data: {
      pipeline_id: pipelineId,
      message_id: messageId,
      ...data,
    },
    source_type: 'system',
    source_id: pipelineId,
    timestamp: new Date().toISOString(),
  })
}
