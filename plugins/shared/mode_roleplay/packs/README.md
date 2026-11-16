# 玩法包（pack）资产目录

出厂播种两件（C3 整合刀，2026-10-13）：`xianxia_combat.yaml`（修仙战斗）与
`stardew_social.yaml`（星露谷好感）。格式：`<id>.yaml`，顶层 `{variables, behaviors,
rules, choices, graph, init_ui, ui}`（variables/behaviors = Tier 0 内置逻辑执行面：
行为匹配 → State Contract Validator；rules/choices/graph = 提示词承载/宿主透出面，
随 `/game/config` 聚合——graph 为剧情流程 Tier 1 键（rp_packs 契约名，聚合后向宿主
透出 `narrative_graph`）；init_ui/ui = 呈现与初始状态面，card_lint lint_ui/lint_init_ui
同源质检）；经卡 yaml 顶层 `pack_ids: [<id>...]` 引用，用户层
`<user_config_dir>/packs/` 同 id 接管。声明面十件标杆与游玩闭环流程真值 =
`docs/working/RP玩法包标杆流程_20261017.md`。

## 玩法内定角色（2026-10-17 用户裁定，ADR 2026-10-17-roleplay-pack-native-ux）

- **包绑定卡 = 内定执行身份**：卡顶层 `pack_ids` 引用包的卡随包开演免选
  （开演设置不渲染选卡件，身份自动锁定）——星露谷绑定世界卡
  `../agents/card_stardew_world.yaml`（星露谷·牧场物语：LLM 扮演世界与固定
  卡司镇民阿比/罗宾，玩家=用户本人），修仙绑定对手卡 `../agents/card_qingyan.yaml`；
- **选角色卡只在组合玩法**：面板「向导开演」（世界+角色组合）与卡详情快捷
  开演保持卡选择；固定玩法零跨包选卡；
- **初始状态归世界卡**：包内 `init_ui`（开局路线/出身词条，effects 键域在本包
  variables 内）即世界卡的初始状态声明位，随包分发、开演即取，不临时配置；
- **卡司卡（NPC）不绑 pack_ids**：`agents/` 同目录出厂卡（tags 含 `npc`，如
  card_abby/card_robin）随包分发但不用 pack_ids 绑定——pack_ids = 执行身份绑定
  面（唯一性归内定卡）；卡司卡消费位 = game_host NPC 深聊区（mode.session 专属
  会话）与场景批 scene_turn participants，主循环数值权威不双结算；
- **世界正典位**：`lorebooks/<pack>_world.yaml` + 内定卡 `lorebook_ids` 绑定
  （material 逐轮关键词命中注入），星露谷/修仙各一件（2026-10-17 标杆补齐）。
