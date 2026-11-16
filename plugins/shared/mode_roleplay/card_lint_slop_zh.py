"""反劣化词表·中文单源（C1 整合刀 2026-10-13；消费方 card_lint.scan_antislop）。

数据纪律（许可三色，与英文表同则）：
- 条目 = 事实性禁词/禁式样，逐条带 ``source`` 考据回链（原件在
  docs/working/rp_prompt_material/ 下同名 .md）；跨源同条目去重合并，
  ``source`` 用 ``+`` 连接多源；
- spiderweb（作者限制性许可：禁商用/二改需授权）与 bartender（许可未明）
  只取词汇条目本身——词条为不具版权性的独立词汇，规则段/范例段一律不进厂。

本表省略项（裁量记录）：
- 单字「神」与日常名词（石头/羽毛/冰块/针）——单字与日常名词误伤率过高，
  规则面归文风档 traits；
- 动词 玩火/插入/燃烧/爆发/溅起——日常合法用法密集，只保留高辨识度的
  剖开/划开/刺穿/戳破/刺破/划破（均带人工判断注记）；
- bartender「那句……的话」句式——日常对话合法形态密集，其套话核心已由
  「悬在空气中」条目覆盖。
扫描语义为 advisory 标注（不阻断）：「突然/一丝/数据」等高频词命中只提示
人工判断，是否采纳归消费面（bartender 原件亦自注「变提词器就关」）。
"""

# 词表条目结构契约（同英文表）：{"term", "note"}——source 在条目行尾注释（说明面，不进数据）
ANTISLOP_ZH_TERMS: tuple[dict[str, str], ...] = (
    # 意象/物品类（spiderweb 与 bartender 禁词表同源合并）
    {"term": "石子", "note": "投湖意象套话"},  # spiderweb-zh-bagu-style-crafts+bartender-zh-agency-slop-guards
    {"term": "涟漪", "note": "心中涟漪套话（场景确有水面则人工放行）"},  # spiderweb-zh-bagu-style-crafts+bartender-zh-agency-slop-guards
    {"term": "深井", "note": "坠井比喻套话"},  # spiderweb-zh-bagu-style-crafts+bartender-zh-agency-slop-guards
    {"term": "黑曜石", "note": "眼眸材质套话"},  # spiderweb-zh-bagu-style-crafts+bartender-zh-agency-slop-guards
    {"term": "方糖", "note": "融化意象套话"},  # spiderweb-zh-bagu-style-crafts+bartender-zh-agency-slop-guards
    {"term": "薄荷糖", "note": "清凉意象套话"},  # spiderweb-zh-bagu-style-crafts+bartender-zh-agency-slop-guards
    {"term": "砂纸", "note": "磨砺比喻套话"},  # spiderweb-zh-bagu-style-crafts+bartender-zh-agency-slop-guards
    {"term": "手术刀", "note": "精准比喻套话（场景确有医疗则人工放行）"},  # spiderweb-zh-bagu-style-crafts+bartender-zh-agency-slop-guards
    {"term": "炸弹", "note": "脑内引爆套话（场景确有爆炸物则人工放行）"},  # spiderweb-zh-bagu-style-crafts+bartender-zh-agency-slop-guards
    # 动作/过程类
    {"term": "剖开", "note": "剖开比喻（高频动词，命中人工判断）"},  # spiderweb-zh-bagu-style-crafts+bartender-zh-agency-slop-guards
    {"term": "划开", "note": "划开比喻（高频动词，命中人工判断）"},  # spiderweb-zh-bagu-style-crafts+bartender-zh-agency-slop-guards
    {"term": "投入水中", "note": "投掷-激发比喻"},  # spiderweb-zh-bagu-style-crafts+bartender-zh-agency-slop-guards
    {"term": "刺穿", "note": "穿刺比喻（高频动词，命中人工判断）"},  # spiderweb-zh-bagu-style-crafts
    {"term": "戳破", "note": "戳破比喻（高频动词，命中人工判断）"},  # spiderweb-zh-bagu-style-crafts
    {"term": "刺破", "note": "刺破比喻（高频动词，命中人工判断）"},  # bartender-zh-agency-slop-guards
    {"term": "划破", "note": "划破比喻（高频动词，命中人工判断）"},  # bartender-zh-agency-slop-guards
    # 形容词/状态类
    {"term": "精准", "note": "机械化形容（高频词，命中人工判断）"},  # spiderweb-zh-bagu-style-crafts+bartender-zh-agency-slop-guards
    {"term": "精确", "note": "机械化形容（高频词，命中人工判断）"},  # spiderweb-zh-bagu-style-crafts+bartender-zh-agency-slop-guards
    {"term": "幽深", "note": "眼眸幽深套话"},  # spiderweb-zh-bagu-style-crafts+bartender-zh-agency-slop-guards
    {"term": "深邃", "note": "眼眸深邃套话"},  # spiderweb-zh-bagu-style-crafts+bartender-zh-agency-slop-guards
    {"term": "细碎", "note": "细碎声响套话（高频词，命中人工判断）"},  # spiderweb-zh-bagu-style-crafts+bartender-zh-agency-slop-guards
    {"term": "戏谑", "note": "戏谑笑意套话"},  # spiderweb-zh-bagu-style-crafts+bartender-zh-agency-slop-guards
    {"term": "霸道", "note": "霸总形容（对话引用则人工放行）"},  # spiderweb-zh-bagu-style-crafts+bartender-zh-agency-slop-guards
    {"term": "难以捉摸", "note": "不可知形容套话"},  # spiderweb-zh-bagu-style-crafts+bartender-zh-agency-slop-guards
    {"term": "不容置疑", "note": "惰性命令词"},  # spiderweb-zh-bagu-style-crafts+bartender-zh-agency-slop-guards
    {"term": "不容置喙", "note": "惰性命令词"},  # spiderweb-zh-bagu-style-crafts
    {"term": "不容拒绝", "note": "惰性命令词"},  # spiderweb-zh-bagu-style-crafts
    {"term": "不容抗拒", "note": "惰性命令词"},  # spiderweb-zh-bagu-style-crafts
    {"term": "声音不大", "note": "音量对比套话"},  # spiderweb-zh-bagu-style-crafts+bartender-zh-agency-slop-guards
    {"term": "声音不高", "note": "音量对比套话"},  # spiderweb-zh-bagu-style-crafts+bartender-zh-agency-slop-guards
    {"term": "陈述", "note": "叙述腔标记（高频词，命中人工判断）"},  # spiderweb-zh-bagu-style-crafts+bartender-zh-agency-slop-guards
    {"term": "肉刃", "note": "器官物化套语"},  # spiderweb-zh-bagu-style-crafts+bartender-zh-agency-slop-guards
    # 冗余/重复词汇族（bartender 补充表）
    {"term": "泛白", "note": "指节泛白套话"},  # bartender-zh-agency-slop-guards
    {"term": "发白", "note": "指节发白套话"},  # bartender-zh-agency-slop-guards
    {"term": "突然", "note": "高频冗余副词（命中人工判断）"},  # bartender-zh-agency-slop-guards
    {"term": "疯狂", "note": "情绪过载形容（温和情绪守卫）"},  # bartender-zh-agency-slop-guards
    {"term": "癫狂", "note": "情绪过载形容（温和情绪守卫）"},  # bartender-zh-agency-slop-guards
    {"term": "一丝", "note": "量词冗余（高频词，命中人工判断）"},  # bartender-zh-agency-slop-guards
    {"term": "闪过", "note": "眼中闪过套话"},  # bartender-zh-agency-slop-guards
    {"term": "生理性的", "note": "生理性反应套话"},  # bartender-zh-agency-slop-guards
    {"term": "弓起", "note": "身体弓起套话"},  # bartender-zh-agency-slop-guards
    {"term": "惊呼", "note": "惊呼出声套话"},  # bartender-zh-agency-slop-guards
    {"term": "雌雄莫辨", "note": "中性美套话"},  # bartender-zh-agency-slop-guards
    {"term": "冷冽", "note": "气息冷冽套话"},  # bartender-zh-agency-slop-guards
    # 神格化/宗教词族（spiderweb CSP-02 与 bartender 防神化同向合并）
    {"term": "造物主", "note": "神格化身份词（宗教设定除外）"},  # spiderweb-zh-bagu-style-crafts
    {"term": "救世主", "note": "神格化身份词（宗教设定除外）"},  # spiderweb-zh-bagu-style-crafts
    {"term": "信徒", "note": "神格化身份词（宗教设定除外）"},  # spiderweb-zh-bagu-style-crafts+bartender-zh-agency-slop-guards
    {"term": "羔羊", "note": "待宰羔羊比喻（命中人工判断）"},  # spiderweb-zh-bagu-style-crafts
    {"term": "朝圣者", "note": "神格化身份词（宗教设定除外）"},  # spiderweb-zh-bagu-style-crafts
    {"term": "造神", "note": "神格化行为词"},  # spiderweb-zh-bagu-style-crafts
    {"term": "膜拜", "note": "神格化行为词"},  # spiderweb-zh-bagu-style-crafts
    {"term": "朝拜", "note": "神格化行为词"},  # spiderweb-zh-bagu-style-crafts
    {"term": "献祭", "note": "神格化行为词（宗教/邪教设定除外）"},  # spiderweb-zh-bagu-style-crafts
    {"term": "仪式", "note": "宗教场合外禁用（日常仪式义命中人工判断）"},  # spiderweb-zh-bagu-style-crafts+bartender-zh-agency-slop-guards
    {"term": "神殿", "note": "神格化场所词（宗教设定除外）"},  # spiderweb-zh-bagu-style-crafts
    {"term": "圣地", "note": "神格化场所词（命中人工判断）"},  # spiderweb-zh-bagu-style-crafts
    {"term": "祭坛", "note": "神格化场所词（宗教设定除外）"},  # spiderweb-zh-bagu-style-crafts+bartender-zh-agency-slop-guards
    {"term": "神谕", "note": "神格化言语词"},  # spiderweb-zh-bagu-style-crafts
    {"term": "福音", "note": "神格化言语词"},  # spiderweb-zh-bagu-style-crafts
    {"term": "圣言", "note": "神格化言语词"},  # spiderweb-zh-bagu-style-crafts
    {"term": "神明", "note": "神格化词（宗教设定除外）"},  # bartender-zh-agency-slop-guards
    {"term": "虔诚", "note": "神格化形容（宗教设定除外）"},  # bartender-zh-agency-slop-guards
    # 石化/绝对静止族（spiderweb DTP-01 与 LDP-01）
    {"term": "石像", "note": "石化描写（石雕语义命中人工判断）"},  # spiderweb-zh-bagu-style-crafts
    {"term": "雕塑", "note": "石化描写（场景确有雕塑则人工放行）"},  # spiderweb-zh-bagu-style-crafts
    {"term": "化石", "note": "石化描写（本义命中人工判断）"},  # spiderweb-zh-bagu-style-crafts
    {"term": "木偶", "note": "石化描写（场景确有木偶则人工放行）"},  # spiderweb-zh-bagu-style-crafts
    {"term": "凝固", "note": "绝对静止夸张（空气凝固套话）"},  # spiderweb-zh-bagu-style-crafts
    {"term": "僵住", "note": "绝对静止夸张（行为中断替代）"},  # spiderweb-zh-bagu-style-crafts
    # 机械化/数据化词族（spiderweb LDP-01 有机思维禁令与 bartender 防机械化同向）
    {"term": "分析", "note": "心智运算化（高频词，命中人工判断）"},  # spiderweb-zh-bagu-style-crafts+bartender-zh-agency-slop-guards
    {"term": "计算", "note": "心智运算化（高频词，命中人工判断）"},  # spiderweb-zh-bagu-style-crafts+bartender-zh-agency-slop-guards
    {"term": "数据", "note": "叙述数据化（工作场合语义命中人工判断）"},  # spiderweb-zh-bagu-style-crafts+bartender-zh-agency-slop-guards
    {"term": "指令", "note": "心智指令化（高频词，命中人工判断）"},  # spiderweb-zh-bagu-style-crafts+bartender-zh-agency-slop-guards
    {"term": "代码", "note": "正文禁代码术语（程序员角色工作场景除外）"},  # bartender-zh-agency-slop-guards
    # 审视性总结语族（bartender 劣质酒精「你很有趣」类）
    {"term": "你很有趣", "note": "居高临下评价语"},  # bartender-zh-agency-slop-guards
    {"term": "事情有趣起来了", "note": "审视性总结语"},  # bartender-zh-agency-slop-guards
    {"term": "不会那么无聊了", "note": "审视性总结语"},  # bartender-zh-agency-slop-guards
    # 情绪过载/神迹化族（bartender 温和情绪与去神化守卫）
    {"term": "前所未有的震撼", "note": "震撼通胀套话"},  # bartender-zh-agency-slop-guards
    {"term": "震撼到失语", "note": "神迹化情绪"},  # bartender-zh-agency-slop-guards
    {"term": "居高临下", "note": "俯视视角套话（身高差不足时物理不成立）"},  # bartender-zh-agency-slop-guards
)

# 禁式样条目结构契约（同英文表）：{"pattern", "note"}——source 在条目行尾注释（说明面，不进数据）
ANTISLOP_ZH_PATTERNS: tuple[dict[str, str], ...] = (
    {"pattern": r"(?:不是|没有|并非)[^。？！]{1,16}而是", "note": "否定肯定结构（对比否定式，含跨逗号形态）"},  # spiderweb-zh-bagu-style-crafts+bartender-zh-agency-slop-guards
    {"pattern": r"像一[颗片道只条枚把座个束]", "note": "「像一……」直白比喻"},  # bartender-zh-agency-slop-guards+spiderweb-zh-bagu-style-crafts
    {"pattern": r"像[^。，？！]{1,10}一样", "note": "「像……一样」明喻（白描档禁令，其他档位人工判断）"},  # spiderweb-zh-bagu-style-crafts
    {"pattern": r"(?:脑海|脑中|颅内|心底|心里|心中)[^。，？！]{0,6}炸开", "note": "瞬间爆发比喻"},  # bartender-zh-agency-slop-guards
    {"pattern": r"投入(?:水中|湖[心中]|海里)", "note": "投掷-激发比喻变体"},  # bartender-zh-agency-slop-guards
    {"pattern": r"量身定制[^。，？！]{0,3}(?:毒药|春药|陷阱)", "note": "模板化赞美套话"},  # bartender-zh-agency-slop-guards
    {"pattern": r"比任何[^。，？！]{1,12}都", "note": "绝对最高级滥用"},  # bartender-zh-agency-slop-guards
    {"pattern": r"就当是[^。]{1,15}吧", "note": "假设性自我安慰句式"},  # bartender-zh-agency-slop-guards
    {"pattern": r"他想[^。]{1,12}也不是[^。]{1,12}至少", "note": "解释性绕弯句式"},  # bartender-zh-agency-slop-guards
    {"pattern": r"声音不[高大][，,]却", "note": "「声音不高，却……」转折定式"},  # bartender-zh-agency-slop-guards
    {"pattern": r"悬在空气[中里]", "note": "话音悬停定式"},  # bartender-zh-agency-slop-guards
)
