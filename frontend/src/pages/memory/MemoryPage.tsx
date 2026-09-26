/**
 * 记忆页（hindsight_memory 声明页 widget memory_panel）
 *
 * 两分区容器：顶部分段控制（全站主题化 Tabs）在「对话记忆」与「文档库」
 * 两个分区间切换，分别挂载 ConversationMemorySection 与 KnowledgeBaseContent。
 * 分区各自自持状态：文档库首次切到才挂载，此后 keep-alive（hidden 切换显隐），
 * 跨分区往返不丢分区状态。
 *
 * 声明面 = hindsight_memory plugin.json 单页 memory；数据面 = hindsight
 * http_endpoints（memory 域 + knowledge-base 域）。
 */

import { useState } from 'react'
import { PageShell } from '@/components/shared/PageShell'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { KnowledgeBaseContent } from '@/pages/knowledge-base/KnowledgeBaseContent'
import { ConversationMemorySection } from './ConversationMemorySection'

/** 分区类型 */
type MemorySection = 'conversation' | 'documents'

/**
 * 记忆页组件（两分区容器）
 */
export function MemoryPage() {
  const [activeSection, setActiveSection] = useState<MemorySection>('conversation')
  // 文档库分区懒挂载标记：首次切到才挂载（避免进页即拉 kb 数据面），此后常驻
  const [documentsMounted, setDocumentsMounted] = useState(false)

  const switchSection = (section: string) => {
    setActiveSection(section as MemorySection)
    if (section === 'documents') {
      setDocumentsMounted(true)
    }
  }

  return (
    <PageShell title="记忆" backHref="/">
      <Tabs value={activeSection} onValueChange={switchSection}>
        <TabsList>
          <TabsTrigger value="conversation">对话记忆</TabsTrigger>
          <TabsTrigger value="documents">文档库</TabsTrigger>
        </TabsList>
      </Tabs>

      {/* 分区面板不用 TabsContent（其卸载非激活态）：hidden keep-alive 保留分区状态 */}
      <section aria-label="对话记忆" hidden={activeSection !== 'conversation'}>
        <ConversationMemorySection />
      </section>
      {documentsMounted && (
        <section aria-label="文档库" hidden={activeSection !== 'documents'}>
          <KnowledgeBaseContent />
        </section>
      )}
    </PageShell>
  )
}
