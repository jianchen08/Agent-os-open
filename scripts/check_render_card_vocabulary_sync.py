#!/usr/bin/env python
"""render.card 卡型词汇表同步闸——生成一致性执法（AR-M5 单源化，E4 同形态）。

单一真值在插件侧声明面（plugins/shared/*/plugin.json 的 render.card +
plugins/shared/dsh_adapter/translator.py 的 DSH 卡型映射），生成物为
frontend/src/utils/renderCardVocabulary.generated.ts（DO-NOT-EDIT，
frontend/src/utils/renderIntent.ts 类型面与声明校验的唯一词表来源）。

本闸执法 = 重渲染与落盘生成物逐字节一致（生成幂等）：手改生成物 / 插件侧
增删卡型未重新生成 / 生成物缺失，任一即退出码 1 并给出再生成指引——
fail-closed，闸绝不允许退化成永绿假闸。

用法：
    python scripts/check_render_card_vocabulary_sync.py    # 绿：退出 0
    # 红：漂移任一 → 退出 1，按指引跑 scripts/generate_render_card_vocabulary.py 再 commit

门禁归属：run_gates.py 的 render-card-sync gate（fast）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from generate_render_card_vocabulary import check_files

if __name__ == "__main__":
    sys.exit(check_files())
