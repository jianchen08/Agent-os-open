/**
 * 时间线唯一事实源：16 条字幕（Godot段字幕.srt）+ 1 个桥段。
 *
 * 时间码口径：原 SRT 2:30–5:30 为本片片段，成片从 0:00 起算、保留原相对间隔，
 * 总 180s = 5400f @30fps。SRT 第 3 条与第 4 条之间原有 6s 空档（原 2:54–3:00），
 * 本片在此放一段品牌桥段（B1），仍属字幕文件之外的空镜，不新增字幕。
 */

export const FPS = 30;

export type Shot = {
  id: string;
  /** 字幕条号（桥段为 null） */
  srt: number | null;
  from: number;
  duration: number;
  /** 字幕文案（= SRT 原文） */
  subtitle: string | null;
  /** 镜头职责（内部制作标注） */
  note: string;
};

export const SHOTS: Shot[] = [
  {id: 'S1', srt: 1, from: 0, duration: 240, subtitle: 'Godot 4 新建项目：一个方块一个场景', note: 'Godot 编辑器登场，方块落地为主角'},
  {id: 'S2', srt: 2, from: 240, duration: 240, subtitle: '游戏本体手动做——AI 不代劳这一步', note: '同一编辑器内近景：手摆的节点与网格'},
  {id: 'S3', srt: 3, from: 480, duration: 240, subtitle: '场景就位。接下来把它交给灵汐', note: '编辑器退为左窗，灵汐入场成右窗'},
  {id: 'B1', srt: null, from: 720, duration: 180, subtitle: null, note: '品牌桥段（原 SRT 空档）：灵汐 AgentOS 字标 hold'},
  {id: 'S4', srt: 4, from: 900, duration: 300, subtitle: '新建会话：Agent 选「Godot 游戏专家」', note: '会话页 + Agent 选择器'},
  {id: 'S5', srt: 5, from: 1200, duration: 450, subtitle: '工作空间填项目目录——专家的改动直接落在这里', note: '工作空间路径逐字输入并锁定'},
  {id: 'S6', srt: 6, from: 1650, duration: 300, subtitle: '不锚定会怎样？专家会如实说「找不到」——隔离边界，不是缺陷', note: '未锚定工作空间的如实报错'},
  {id: 'S7', srt: 7, from: 1950, duration: 450, subtitle: '会话就绪。回 Godot 选中场景节点——引用卡片即时出现', note: '选中节点 → 引用卡片飞入输入区'},
  {id: 'S8', srt: 8, from: 2400, duration: 240, subtitle: '引用自带节点坐标——选中即上下文，不用贴路径', note: '引用卡片展开：类型/路径/坐标'},
  {id: 'S9', srt: 9, from: 2640, duration: 300, subtitle: '文件也行：Dock 里选中，五秒内生效', note: 'FileSystem Dock 选中文件 → 引用卡片'},
  {id: 'S10', srt: 10, from: 2940, duration: 240, subtitle: '点 × 消失——再点物品，恢复', note: '清除引用 → 重新选中恢复'},
  {id: 'S11', srt: 11, from: 3180, duration: 360, subtitle: '发消息：把玩家挪到右上角，再换个颜色', note: '逐字输入需求 + 回车发送'},
  {id: 'S12', srt: 12, from: 3540, duration: 360, subtitle: '引用随消息一起发给专家——卡片消失=已消耗', note: '引用随消息发出，输入区清空'},
  {id: 'S13', srt: 13, from: 3900, duration: 360, subtitle: '专家读引用定位节点，直接改场景文件', note: '专家按引用定位 + 场景文件 diff'},
  {id: 'S14', srt: 14, from: 4260, duration: 360, subtitle: 'Godot 自动重载——方块真的动了', note: '编辑器重载，方块移动并换色'},
  {id: 'S15', srt: 15, from: 4620, duration: 360, subtitle: 'F5 试玩：改的就是它', note: 'F5 运行，游戏窗口里就是改后的方块'},
  {id: 'S16', srt: 16, from: 4980, duration: 420, subtitle: '从选中到改完——内核没动过', note: '收尾合影 + 字标 + 内核零改动'},
];

export const TOTAL_FRAMES = 5400;

export const shot = (id: string): Shot => {
  const s = SHOTS.find((x) => x.id === id);
  if (!s) throw new Error(`unknown shot ${id}`);
  return s;
};

/** 相对镜头起点取帧（SFX 钉帧与场景动作共用，镜头起点变化时自动跟随） */
export const at = (id: string, offset: number) => shot(id).from + offset;

/** 逐字打字的时间表：给定文本与起止帧，返回第 i 个字出现的帧 */
export const typeFrames = (start: number, end: number, len: number) =>
  Array.from({length: len}, (_, i) => start + ((end - start) * i) / Math.max(1, len - 1));
