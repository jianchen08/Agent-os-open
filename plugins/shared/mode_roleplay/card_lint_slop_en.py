"""反劣化词表·英文单源（C1 整合刀 2026-10-13；消费方 card_lint.scan_antislop）。

数据纪律（许可三色）：
- 条目 = 事实性禁词/禁式样（词汇与短语本身不受版权保护），逐条带 ``source``
  考据回链（原件在 docs/working/rp_prompt_material/ 下同名 .md）；
- 跨源同条目去重合并，``source`` 用 ``+`` 连接多源；
- 规则段/示例文案不进厂：dem（CC-BY-NC-SA）/ atelier / stabs（许可未明）只取
  词汇条目；wbu（MIT）可改编； aids（CC BY-SA）负向措辞检测面向提示词组装
  面（prompt-craft），不进正文词表（对话误伤），其对比否定式关切已由
  contrastive negation 条目家族覆盖。

本表省略项（不可 lint 或误伤率过高的裁量，规则面归文风档 traits）：
- dem 标点禁令（分号/冒号）——正常行文必命中；
- stabs 堆叠形容词（2+ 逗号连排形容词）与 "More than X" 族——需句法分析；
- stabs "toaster"——语境道具，孤词无可判定语义；
- atelier 裸 hedge 词（almost / somewhat / as if / as though）——日常高频，
  只保留特征短语（a flicker of / a hint of / seemed to）；
- dem NSFW 台词中露骨变体一条（milking your...）——扫描面为共享基础设施，
  从略，其余台词套话照收。
"""

# 词表条目结构契约：{"term": 禁词/禁短语（大小写不敏感子串匹配）,
#                 "source": 考据回链（多源 + 连接）, "note": 一句话命中说明}
ANTISLOP_EN_TERMS: tuple[dict[str, str], ...] = (
    {"term": "ozone", "note": "雷雨气味套话"},  # dem-anti-slop-suite+stabs-anti-slop
    {"term": "slick folds", "note": "身体组织套语"},  # dem-anti-slop-suite
    {"term": "white knuckles", "note": "攥拳特写套话"},  # dem-anti-slop-suite
    {"term": "predatory", "note": "捕食者形容（高频词，命中人工判断）"},  # dem-anti-slop-suite+stabs-anti-slop
    {"term": "unadulterated", "note": "纯粹化形容"},  # dem-anti-slop-suite
    {"term": "jasmine", "note": "香气套话（场景确有茉莉则人工放行）"},  # dem-anti-slop-suite
    {"term": "calculating", "note": "算计形容（高频词，命中人工判断）"},  # dem-anti-slop-suite
    {"term": "deliberate", "note": "从容形容（高频词，命中人工判断）"},  # dem-anti-slop-suite+stabs-anti-slop
    {"term": "really sees me", "note": "被看见套话"},  # dem-anti-slop-suite
    {"term": "usual bite", "note": "失去锋芒套话"},  # dem-anti-slop-suite
    {"term": "nobody's ever", "note": "从未有人句式"},  # dem-anti-slop-suite
    {"term": "no one's ever", "note": "从未有人句式变体"},  # dem-anti-slop-suite
    {"term": "no one ever said", "note": "从未有人说变体"},  # dem-anti-slop-suite+wbu-anti-slop
    {"term": "something older", "note": "古老之物套话"},  # dem-anti-slop-suite
    {"term": "not a question", "note": "不是疑问套话"},  # dem-anti-slop-suite+stabs-anti-slop
    {"term": "did you just", "note": "惊问套话"},  # dem-anti-slop-suite
    {"term": "don't you dare stop", "note": "NSFW 台词套话"},  # dem-anti-slop-suite
    {"term": "forget my own name", "note": "NSFW 台词套话"},  # dem-anti-slop-suite
    {"term": "ruin me for anyone else", "note": "NSFW 台词套话"},  # dem-anti-slop-suite
    {"term": "outside, somewhere", "note": "远景切换套话"},  # dem-anti-slop-suite
    {"term": "arousal pooling", "note": "情欲体内描写套话"},  # stabs-anti-slop
    {"term": "torn between", "note": "撕扯形容"},  # stabs-anti-slop
    {"term": "pupils blown", "note": "瞳孔特写套话"},  # stabs-anti-slop
    {"term": "analytical gaze", "note": "分析式目光"},  # stabs-anti-slop
    {"term": "barely whisper", "note": "压低嗓音套话（含 barely a whisper）"},  # stabs-anti-slop
    {"term": "pregnant pause", "note": "意味深长停顿"},  # stabs-anti-slop
    {"term": "economical movements", "note": "省力动作形容"},  # stabs-anti-slop
    {"term": "escapes me", "note": "X escapes me 句式核心"},  # stabs-anti-slop
    {"term": "i'm aware of", "note": "自我觉察声明"},  # stabs-anti-slop
    {"term": "i can feel", "note": "可见感受声明（高频短语，命中人工判断）"},  # stabs-anti-slop
    {"term": "playful challenge", "note": "俏皮挑战套话"},  # stabs-anti-slop
    {"term": "stark contrast", "note": "鲜明对比套话"},  # stabs-anti-slop
    {"term": "the night is young", "note": "夜还长套话"},  # stabs-anti-slop
    {"term": "crimson beads", "note": "血珠描写套话"},  # stabs-anti-slop
    {"term": "choice is yours", "note": "交还选择权套话"},  # stabs-anti-slop
    {"term": "words are flat", "note": "声音扁平形容"},  # stabs-anti-slop
    {"term": "warm haze", "note": "模糊暖雾套话"},  # stabs-anti-slop
    {"term": "soft blur", "note": "柔焦套话"},  # stabs-anti-slop
    {"term": "pleasant fog", "note": "雾化套话"},  # stabs-anti-slop
    {"term": "for a long moment", "note": "悬停计时套话"},  # stabs-anti-slop
    {"term": "in this moment", "note": "此刻强调套话"},  # stabs-anti-slop
    {"term": "in that moment", "note": "此刻强调套话"},  # stabs-anti-slop
    {"term": "did not look back", "note": "不回头特写（高频短语，命中人工判断）"},  # stabs-anti-slop
    {"term": "silence stretched", "note": "沉默拉长套话"},  # stabs-anti-slop+nemo-atelier-prose-contract
    {"term": "a flicker of", "note": "闪烁 hedge"},  # nemo-atelier-prose-contract
    {"term": "a hint of", "note": "一丝 hedge（英文）"},  # nemo-atelier-prose-contract
    {"term": "palpable", "note": "可触摸形容"},  # nemo-atelier-prose-contract
    {"term": "a testament to", "note": "见证腔"},  # nemo-atelier-prose-contract
    {"term": "the weight of the moment", "note": "时刻之重套话"},  # nemo-atelier-prose-contract
    {"term": "heart hammered", "note": "心脏擂鼓套话"},  # nemo-atelier-prose-contract
    # 套皮命名族（dem：新建非既有角色命中=建议换名；常见真名命中人工判断）
    {"term": "Elara", "note": "套皮命名（新建角色建议换名）"},  # dem-anti-slop-suite
    {"term": "Marcus", "note": "套皮命名（同上）"},  # dem-anti-slop-suite
    {"term": "Nora", "note": "套皮命名（同上）"},  # dem-anti-slop-suite
    {"term": "Riley", "note": "套皮命名（同上）"},  # dem-anti-slop-suite
    {"term": "Vance", "note": "套皮命名（同上）"},  # dem-anti-slop-suite
    {"term": "Sloane", "note": "套皮命名（同上）"},  # dem-anti-slop-suite
    {"term": "Vex", "note": "套皮命名（同上）"},  # dem-anti-slop-suite
    {"term": "Voss", "note": "套皮命名（同上）"},  # dem-anti-slop-suite
    {"term": "Thorne", "note": "套皮命名（同上）"},  # dem-anti-slop-suite
    {"term": "Seraphine", "note": "套皮命名（同上）"},  # dem-anti-slop-suite
    {"term": "Seraphina", "note": "套皮命名（同上）"},  # dem-anti-slop-suite
    {"term": "Lily", "note": "套皮命名（高频真名，命中人工判断）"},  # dem-anti-slop-suite
    {"term": "Chen", "note": "套皮命名（高频真姓，命中人工判断）"},  # dem-anti-slop-suite
    {"term": "Eldoria", "note": "套皮地名"},  # dem-anti-slop-suite
    {"term": "Meridian", "note": "套皮地名"},  # dem-anti-slop-suite
    {"term": "Thornwood", "note": "套皮地名"},  # dem-anti-slop-suite
)

# 禁式样条目结构契约：{"pattern": 正则（IGNORECASE 编译）, "source": 回链,
#                    "note": 命中说明}
ANTISLOP_EN_PATTERNS: tuple[dict[str, str], ...] = (
    {"pattern": r"shivers?(?: \w+)? down (?:my|his|her|your|their|the) spine", "note": "脊背发凉套话（含 shiver ran/crawled down 变体）"},  # dem-anti-slop-suite+stabs-anti-slop
    {"pattern": r"mouths? opens?\.?\s*closes\.?", "note": "张嘴又闭上特写"},  # dem-anti-slop-suite+wbu-anti-slop
    {"pattern": r"going to break (?:him|her|you|them)\b", "note": "弄坏你台词套话"},  # dem-anti-slop-suite
    {"pattern": r"through (?:her|his|their|your) lashes", "note": "抬眼睨视套话"},  # dem-anti-slop-suite
    {"pattern": r"like a physical blow", "note": "实体冲击比喻"},  # dem-anti-slop-suite+stabs-anti-slop
    {"pattern": r"(?:the )?only solid thing", "note": "唯一实存比喻"},  # dem-anti-slop-suite
    {"pattern": r"\ba beat\b", "note": "悬停拍子（高频短语，命中人工判断）"},  # dem-anti-slop-suite
    {"pattern": r"\b(?:a|the) (?:beat|moment) passed\b", "note": "一拍过去套话"},  # dem-anti-slop-suite+nemo-atelier-prose-contract+stabs-anti-slop
    {"pattern": r"breath (?:hitch(?:es|ed)|catch(?:es|ing))", "note": "呼吸一滞套话"},  # dem-anti-slop-suite+stabs-anti-slop
    {"pattern": r"\bone, two\b|\bonce, twice\b", "note": "计数节奏套话"},  # dem-anti-slop-suite
    {"pattern": r"\bit'?s not [^.;!?\n]{1,40}[,;] it'?s\b", "note": "对比否定式（epanorthosis）"},  # dem-anti-slop-suite+wbu-anti-slop
    {"pattern": r"\bnot (?:just|only|merely)[^.!?\n]{1,50}\bbut\b", "note": "否定平行式（not X but Y）"},  # dem-anti-slop-suite+wbu-anti-slop
    {"pattern": r"and bad decisions", "note": "坏决定后缀套话"},  # dem-anti-slop-suite
    {"pattern": r"\b(?:repeat|repeats|repeated|echo|echoes|echoed|taste|tastes|tasted|tasting) the word\b", "note": "引词反应（角色复读关键词）"},  # dem-anti-slop-suite
    {"pattern": r"\bso that means\b", "note": "论文腔推论"},  # dem-anti-slop-suite
    {"pattern": r"\bhit like (?:a |an )?\w+", "note": "如遭击打比喻"},  # stabs-anti-slop
    {"pattern": r"\b(?:world|universe|reality|existence) (?:tilts?|tilted|narrows?|narrowed|tips|tipped|stops?|stopped|falls? away|fell away|contracts?|contracted)\b", "note": "世界缩窄/倾侧族"},  # stabs-anti-slop
    {"pattern": r"\beverything else (?:fades?|faded|ceases?|ceased|disappears?|disappeared|falls? away|fell away)\b", "note": "万物退散族"},  # stabs-anti-slop
    {"pattern": r"\bair (?:was|is|grew|hung|felt|seemed)? ?(?:thick|heavy|charged|crackl(?:e|ed|ing))\b", "note": "空气凝滞族"},  # stabs-anti-slop
    {"pattern": r"\bface (?:was |is |a )?(?:mask|canvas)\b", "note": "面孔画布比喻"},  # stabs-anti-slop
    {"pattern": r"\bexpression (?:\w+ )?(?:neutral|blank|unreadable)\b", "note": "表情不可读套话"},  # stabs-anti-slop
    {"pattern": r"\bline of (?:his|her|their|your) (?:mouth|jaw)", "note": "嘴线/颌线特写"},  # stabs-anti-slop
    {"pattern": r"\b(?:velvety|silky|gravelly|husky|breathy|throaty|guttural) voice\b", "note": "嗓音质地族"},  # stabs-anti-slop
    {"pattern": r"\bpurr(?:ed|ing|s)?\b|\bgrowl(?:ed|ing|s)?\b", "note": "嗓音动物化（高频词，命中人工判断）"},  # stabs-anti-slop
    {"pattern": r"\b(?:fluid|smooth|measured) and (?:precise|efficient|controlled)\b", "note": "流畅精准对仗"},  # stabs-anti-slop
    {"pattern": r"\blet (?:the )?(?:word|words|silence|question|moment|pause)s? hang\b", "note": "让话悬着套话"},  # stabs-anti-slop
    {"pattern": r"\btime (?:seemed|appeared) to (?:slow|stop|stand still)\b|\btime (?:stopped|slowed|froze)\b", "note": "时间减速套话"},  # stabs-anti-slop+nemo-atelier-prose-contract
    {"pattern": r"\b(?:real|genuine|true) (?:fear|anger|smile|laugh|warmth|danger|threat|emotion|feeling|affection|curiosity|amusement|pain|relief|wonder|dread)\b", "note": "真实情绪强调式"},  # stabs-anti-slop
    {"pattern": r"\b(?:was|became) (?:a|an) [\w-]+ thing\b", "note": "模糊之物句式（高频句型，命中人工判断）"},  # stabs-anti-slop
    {"pattern": r"\bterrible,? beautiful\b|\bpainful,? perfect\b|\bcruel,? kind\b", "note": "矛盾形容对"},  # stabs-anti-slop
    {"pattern": r"\buniquely (?:his|her|their|your|yours|mine)\b", "note": "独属强调"},  # stabs-anti-slop
    {"pattern": r"\b(?:seem|seems|seemed) to\b", "note": "seemed hedge（高频词，命中人工判断）"},  # nemo-atelier-prose-contract
    {"pattern": r"\ba wave of [\w-]+ (?:washed(?: over)?|crashed|rolled(?: over)?|swept(?: over)?)\b", "note": "情绪浪潮套话"},  # nemo-atelier-prose-contract
    {"pattern": r"\bblood (?:ran|run|turns|turned|drained) cold\b", "note": "血液变冷套话"},  # nemo-atelier-prose-contract
)
