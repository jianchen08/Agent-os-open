/*
 * Ported from DeepSeek Harness (DSH) — MIT License.
 * Copyright (c) 2026 DeepSeek. Source: packages/client/ui-primitives/src/use-copy-feedback.ts
 * Repo pinned at commit 47f943859bef60e4160492346772ded9b24f765a (version 0.1.0-rc.5). Adapted for AgentOS
 * (task_dsh_plugin_adapter 任务 3): verbatim copy.
 */
// The copy-to-clipboard-with-feedback hook shared by the block primitives
// (TerminalBlock, SearchBlock): write the given text, and on success flip a
// transient `copied` flag that the caller renders as a "复制成功" label for one
// second. A refused write leaves the flag untouched, so the control never claims
// a copy the host declined.

import { useCallback, useState } from 'react'
import { writeClipboard } from './clipboard.ts'

/** How long the `copied` flag stays true after a successful write, in ms. */
const COPIED_FEEDBACK_MS = 1000

/** The copy-feedback hook's return: the transient flag and the copy handler. */
export interface CopyFeedback {
  /** True for {@link COPIED_FEEDBACK_MS} after a successful write; render the success label off it. */
  copied: boolean
  /** Copy the hook's text; no-op while `copied` is still true, silent on a refused write. */
  onCopy: () => void
}

/**
 * Copy `text` to the clipboard with one-second success feedback.
 * @param text - the text to write on copy.
 * @returns the `copied` flag and the `onCopy` handler.
 */
export function useCopyFeedback(text: string): CopyFeedback {
  const [copied, setCopied] = useState(false)
  const onCopy = useCallback(() => {
    if (copied) return
    void writeClipboard(text).then((ok) => {
      if (!ok) return
      setCopied(true)
      window.setTimeout(() => { setCopied(false) }, COPIED_FEEDBACK_MS)
    })
  }, [copied, text])
  return { copied, onCopy }
}
