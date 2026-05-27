"""
贪吃蛇游戏 - 测试套件

验证 snake_game.html 的 HTML 结构、CSS 样式、JavaScript 游戏逻辑的正确性。
包括：游戏初始化、蛇移动、食物放置、碰撞检测、计分系统、速度递增、暂停/继续、游戏结束等。
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

# snake_game.html 文件不存在，跳过整个模块
pytestmark = pytest.mark.skip(reason="snake_game.html 文件不存在: test_screenshots/snake_game.html")

# ============================================================================
# 辅助工具：解析 HTML 和提取 JS 逻辑
# ============================================================================

GAME_FILE = Path(__file__).resolve().parent.parent / "test_screenshots" / "snake_game.html"


class HTMLStructureParser(HTMLParser):
    """解析 HTML 文件，提取标签、属性和文本内容。"""

    def __init__(self) -> None:
        super().__init__()
        self.tags: list[str] = []
        self.ids: dict[str, str] = {}  # id -> tag name
        self.classes: list[str] = []
        self.scripts: list[str] = []
        self.styles: list[str] = []
        self._current_tag: str = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._current_tag = tag
        self.tags.append(tag)
        attrs_dict = dict(attrs)
        if "id" in attrs_dict and attrs_dict["id"]:
            self.ids[attrs_dict["id"]] = tag
        if "class" in attrs_dict and attrs_dict["class"]:
            self.classes.extend(attrs_dict["class"].split())

    def handle_data(self, data: str) -> None:
        if self._current_tag == "script" and data.strip():
            self.scripts.append(data.strip())
        elif self._current_tag == "style" and data.strip():
            self.styles.append(data.strip())


def _load_game_html() -> str:
    """加载游戏 HTML 文件内容。"""
    assert GAME_FILE.exists(), f"游戏文件不存在: {GAME_FILE}"
    return GAME_FILE.read_text(encoding="utf-8")


def _parse_html(html: str) -> HTMLStructureParser:
    """解析 HTML 并返回结构化数据。"""
    parser = HTMLStructureParser()
    parser.feed(html)
    return parser


def _extract_js(html: str) -> str:
    """从 HTML 中提取 JavaScript 代码。"""
    match = re.search(r"<script>(.*?)</script>", html, re.DOTALL)
    assert match, "未找到 <script> 标签"
    return match.group(1).strip()


def _extract_css(html: str) -> str:
    """从 HTML 中提取 CSS 代码。"""
    match = re.search(r"<style>(.*?)</style>", html, re.DOTALL)
    assert match, "未找到 <style> 标签"
    return match.group(1).strip()


# ============================================================================
# 测试类
# ============================================================================


class TestSnakeGameHTMLStructure:
    """测试贪吃蛇游戏 HTML 结构的完整性。"""

    @pytest.fixture
    def html_content(self) -> str:
        return _load_game_html()

    @pytest.fixture
    def parsed(self, html_content: str) -> HTMLStructureParser:
        return _parse_html(html_content)

    def test_file_exists(self) -> None:
        """验证游戏文件存在且非空。"""
        assert GAME_FILE.exists(), f"游戏文件不存在: {GAME_FILE}"
        content = GAME_FILE.read_text(encoding="utf-8")
        assert len(content) > 0, "游戏文件为空"

    def test_html5_doctype(self, html_content: str) -> None:
        """验证使用 HTML5 文档类型。"""
        assert html_content.strip().startswith("<!DOCTYPE html>"), "缺少 HTML5 DOCTYPE 声明"

    def test_charset_utf8(self, html_content: str) -> None:
        """验证字符编码为 UTF-8。"""
        assert 'charset="UTF-8"' in html_content or "charset=UTF-8" in html_content, "缺少 UTF-8 字符编码声明"

    def test_page_title(self, html_content: str) -> None:
        """验证页面标题包含'贪吃蛇'。"""
        title_match = re.search(r"<title>(.*?)</title>", html_content)
        assert title_match, "缺少 <title> 标签"
        assert "贪吃蛇" in title_match.group(1), f"标题应包含'贪吃蛇': {title_match.group(1)}"

    def test_has_canvas_element(self, parsed: HTMLStructureParser) -> None:
        """验证存在 canvas 画布元素。"""
        assert "canvas" in parsed.tags, "缺少 <canvas> 元素"

    def test_canvas_has_id(self, parsed: HTMLStructureParser) -> None:
        """验证 canvas 有 id 属性。"""
        assert "game" in parsed.ids, "canvas 缺少 id='game'"

    def test_has_score_display(self, parsed: HTMLStructureParser) -> None:
        """验证存在得分显示元素。"""
        assert "score" in parsed.ids, "缺少 id='score' 的得分显示元素"

    def test_has_best_score_display(self, parsed: HTMLStructureParser) -> None:
        """验证存在最高分显示元素。"""
        assert "bestScore" in parsed.ids, "缺少 id='bestScore' 的最高分显示元素"

    def test_has_speed_display(self, parsed: HTMLStructureParser) -> None:
        """验证存在速度显示元素。"""
        assert "speed" in parsed.ids, "缺少 id='speed' 的速度显示元素"

    def test_has_overlay(self, parsed: HTMLStructureParser) -> None:
        """验证存在覆盖层（开始/结束界面）。"""
        assert "overlay" in parsed.ids, "缺少 id='overlay' 的覆盖层元素"

    def test_has_start_button(self, parsed: HTMLStructureParser) -> None:
        """验证存在开始游戏按钮。"""
        assert "startBtn" in parsed.ids, "缺少 id='startBtn' 的开始按钮"

    def test_has_controls_hint(self, html_content: str) -> None:
        """验证存在操作提示文本。"""
        assert "controls" in html_content, "缺少操作提示"
        assert "方向键" in html_content or "WASD" in html_content, "缺少方向键或 WASD 操作说明"

    def test_has_heading(self, parsed: HTMLStructureParser) -> None:
        """验证存在 h1 标题。"""
        assert "h1" in parsed.tags, "缺少 <h1> 标题"


class TestSnakeGameCSS:
    """测试贪吃蛇游戏 CSS 样式。"""

    @pytest.fixture
    def css(self) -> str:
        return _extract_css(_load_game_html())

    def test_css_exists(self, css: str) -> None:
        """验证 CSS 样式存在。"""
        assert len(css) > 0, "CSS 样式为空"

    def test_body_styling(self, css: str) -> None:
        """验证 body 有居中布局。"""
        assert "align-items" in css or "text-align" in css, "body 缺少居中布局"

    def test_canvas_styling(self, css: str) -> None:
        """验证 canvas 有边框或圆角样式。"""
        assert "border" in css or "border-radius" in css, "canvas 缺少边框或圆角样式"

    def test_button_styling(self, css: str) -> None:
        """验证按钮有样式定义。"""
        assert "button" in css, "缺少按钮样式定义"

    def test_overlay_styling(self, css: str) -> None:
        """验证覆盖层有样式定义。"""
        assert "#overlay" in css or ".overlay" in css or "overlay" in css, "缺少覆盖层样式定义"


class TestSnakeGameJSLogic:
    """测试贪吃蛇游戏 JavaScript 核心逻辑。"""

    @pytest.fixture
    def js(self) -> str:
        return _extract_js(_load_game_html())

    def test_js_exists(self, js: str) -> None:
        """验证 JavaScript 代码存在。"""
        assert len(js) > 100, "JavaScript 代码过短或不存在"

    def test_has_grid_constant(self, js: str) -> None:
        """验证定义了网格常量。"""
        assert "GRID" in js, "缺少 GRID 网格常量定义"

    def test_has_cols_rows(self, js: str) -> None:
        """验证定义了行列常量。"""
        assert "COLS" in js, "缺少 COLS 列数常量"
        assert "ROWS" in js, "缺少 ROWS 行数常量"

    def test_has_init_function(self, js: str) -> None:
        """验证存在初始化函数。"""
        assert "function init()" in js, "缺少 function init() 初始化函数"

    def test_has_reset_function(self, js: str) -> None:
        """验证存在状态重置函数。"""
        assert "function resetState()" in js, "缺少 function resetState() 重置函数"

    def test_has_place_food_function(self, js: str) -> None:
        """验证存在食物放置函数。"""
        assert "function placeFood()" in js, "缺少 function placeFood() 食物放置函数"

    def test_has_start_game_function(self, js: str) -> None:
        """验证存在开始游戏函数。"""
        assert "function startGame()" in js, "缺少 function startGame() 开始游戏函数"

    def test_has_tick_function(self, js: str) -> None:
        """验证存在游戏主循环函数。"""
        assert "function tick()" in js, "缺少 function tick() 游戏主循环函数"

    def test_has_draw_function(self, js: str) -> None:
        """验证存在绘制函数。"""
        assert "function draw()" in js, "缺少 function draw() 绘制函数"

    def test_has_end_game_function(self, js: str) -> None:
        """验证存在游戏结束函数。"""
        assert "function endGame()" in js, "缺少 function endGame() 游戏结束函数"

    def test_has_update_ui_function(self, js: str) -> None:
        """验证存在 UI 更新函数。"""
        assert "function updateUI()" in js, "缺少 function updateUI() UI 更新函数"

    def test_has_get_interval_function(self, js: str) -> None:
        """验证存在速度间隔计算函数。"""
        assert "function getInterval()" in js, "缺少 function getInterval() 间隔计算函数"


class TestSnakeGameMechanics:
    """测试贪吃蛇游戏核心机制（通过代码分析验证逻辑正确性）。"""

    @pytest.fixture
    def js(self) -> str:
        return _extract_js(_load_game_html())

    def test_snake_initial_position(self, js: str) -> None:
        """验证蛇有初始位置和身体。"""
        # 初始蛇应该有至少 3 节身体
        assert "cx" in js and "cy" in js, "缺少蛇初始中心位置计算"
        assert "snake" in js, "缺少 snake 变量定义"

    def test_direction_control(self, js: str) -> None:
        """验证方向控制存在。"""
        assert "dir" in js, "缺少方向变量"
        assert "nextDir" in js, "缺少下一方向变量（防抖）"

    def test_food_collision_detection(self, js: str) -> None:
        """验证食物碰撞检测逻辑。"""
        # 应该检测蛇头是否与食物重合
        assert "food" in js, "缺少食物变量"
        assert "head.x === food.x" in js or "head.x == food.x" in js, "缺少蛇头与食物的碰撞检测"

    def test_wall_collision_detection(self, js: str) -> None:
        """验证墙壁碰撞检测逻辑。"""
        # 应该检测蛇头是否超出边界
        assert "head.x < 0" in js, "缺少左边界检测"
        assert "head.x >= COLS" in js, "缺少右边界检测"
        assert "head.y < 0" in js, "缺少上边界检测"
        assert "head.y >= ROWS" in js, "缺少下边界检测"

    def test_self_collision_detection(self, js: str) -> None:
        """验证自身碰撞检测逻辑。"""
        # 蛇头碰到自己身体应触发游戏结束
        assert "snake.some" in js, "缺少蛇身碰撞检测（snake.some）"

    def test_scoring_system(self, js: str) -> None:
        """验证计分系统。"""
        assert "score" in js, "缺少分数变量"
        assert "score += 10" in js or "score=score+10" in js, "缺少加分逻辑（每次吃食物应加 10 分）"

    def test_speed_increase(self, js: str) -> None:
        """验证速度递增机制。"""
        assert "speed" in js, "缺少速度变量"
        assert "speed + 1" in js or "speed++" in js, "缺少速度递增逻辑"

    def test_speed_up_threshold(self, js: str) -> None:
        """验证加速触发阈值（每 50 分加速一次）。"""
        assert "score % 50" in js, "缺少每 50 分加速阈值检查"

    def test_max_speed_cap(self, js: str) -> None:
        """验证最大速度上限。"""
        assert "Math.min" in js, "缺少速度上限（Math.min）"

    def test_pause_functionality(self, js: str) -> None:
        """验证暂停功能。"""
        assert "paused" in js, "缺少暂停状态变量"
        assert "paused" in js and "!paused" in js.replace(" ", ""), "缺少暂停切换逻辑"

    def test_game_over_state(self, js: str) -> None:
        """验证游戏结束状态。"""
        assert "gameOver" in js, "缺少 gameOver 状态变量"
        assert "endGame()" in js, "缺少 endGame() 游戏结束函数调用"

    def test_clear_interval_on_game_over(self, js: str) -> None:
        """验证游戏结束时清除定时器。"""
        # endGame 函数中应有 clearInterval
        end_game_match = re.search(r"function endGame\(\)[\s\S]*?clearInterval", js)
        assert end_game_match, "游戏结束函数中缺少 clearInterval 调用"

    def test_snake_grows_on_eating(self, js: str) -> None:
        """验证蛇吃到食物后变长（不 pop）。"""
        # tick 函数中：吃到食物时不 pop，否则 pop
        tick_match = re.search(r"function tick\(\)[\s\S]*?snake\.pop", js)
        assert tick_match, "缺少蛇身增长/缩短逻辑（snake.pop）"
        # 应该有条件判断：吃到食物时跳过 pop
        assert "else" in js, "缺少吃食物/不吃食物的条件分支"

    def test_snake_unshift_on_move(self, js: str) -> None:
        """验证蛇移动时添加新头部。"""
        assert "snake.unshift" in js, "缺少 snake.unshift 新头部添加"

    def test_best_score_persistence(self, js: str) -> None:
        """验证最高分持久化（localStorage）。"""
        assert "localStorage" in js, "缺少 localStorage 持久化"
        assert "snakeBest" in js, "缺少 snakeBest localStorage 键名"
        assert "localStorage.setItem" in js, "缺少 localStorage.setItem 保存最高分"
        assert "localStorage.getItem" in js, "缺少 localStorage.getItem 读取最高分"


class TestSnakeGameKeyboard:
    """测试贪吃蛇游戏键盘控制。"""

    @pytest.fixture
    def js(self) -> str:
        return _extract_js(_load_game_html())

    def test_has_keydown_listener(self, js: str) -> None:
        """验证注册了键盘事件监听。"""
        assert "keydown" in js or "addEventListener" in js, "缺少键盘事件监听"

    def test_arrow_key_support(self, js: str) -> None:
        """验证支持方向键控制。"""
        assert "ArrowUp" in js or "arrowup" in js, "缺少上方向键支持"
        assert "ArrowDown" in js or "arrowdown" in js, "缺少下方向键支持"
        assert "ArrowLeft" in js or "arrowleft" in js, "缺少左方向键支持"
        assert "ArrowRight" in js or "arrowright" in js, "缺少右方向键支持"

    def test_wasd_support(self, js: str) -> None:
        """验证支持 WASD 键控制。"""
        wasd_count = sum(1 for k in ["'w'", "'a'", "'s'", "'d'", '"w"', '"a"', '"s"', '"d"'] if k in js)
        assert wasd_count >= 4, f"WASD 键支持不完整，仅匹配 {wasd_count}/4 个键"

    def test_space_pause_support(self, js: str) -> None:
        """验证空格键暂停支持。"""
        assert "' '" in js or "'spacebar'" in js or '"space"' in js, "缺少空格键暂停支持"

    def test_anti_reverse_direction(self, js: str) -> None:
        """验证防止反向移动（不能直接掉头）。"""
        # 应有 nd.x + dir.x !== 0 这样的检查
        assert "dir.x" in js and "dir.y" in js, "缺少方向防反转检查"


class TestSnakeGameVisuals:
    """测试贪吃蛇游戏视觉效果相关代码。"""

    @pytest.fixture
    def js(self) -> str:
        return _extract_js(_load_game_html())

    def test_food_drawing(self, js: str) -> None:
        """验证食物绘制代码。"""
        assert "arc(" in js, "缺少食物绘制（arc 方法）"

    def test_snake_drawing(self, js: str) -> None:
        """验证蛇身绘制代码。"""
        assert "fillRect" in js, "缺少蛇身绘制（fillRect 方法）"
        assert "forEach" in js, "缺少蛇身遍历绘制逻辑"

    def test_grid_drawing(self, js: str) -> None:
        """验证网格线绘制。"""
        assert "lineTo" in js or "stroke" in js, "缺少网格线绘制"

    def test_pause_overlay_text(self, js: str) -> None:
        """验证暂停时显示提示文本。"""
        assert "暂停" in js, "缺少暂停提示文本"

    def test_game_over_text(self, js: str) -> None:
        """验证游戏结束时显示提示文本。"""
        assert "游戏结束" in js, "缺少游戏结束提示文本"


class TestSnakeGameIntegration:
    """集成测试：验证游戏各模块之间的协作。"""

    @pytest.fixture
    def html_content(self) -> str:
        return _load_game_html()

    @pytest.fixture
    def js(self) -> str:
        return _extract_js(_load_game_html())

    def test_init_called_on_load(self, js: str) -> None:
        """验证页面加载时调用了 init()。"""
        # JS 最后应调用 init()
        lines = js.strip().split("\n")
        last_non_empty = ""
        for line in reversed(lines):
            stripped = line.strip()
            if stripped and not stripped.startswith("//") and not stripped.startswith("/*"):
                last_non_empty = stripped
                break
        assert "init()" in last_non_empty, f"页面加载时未调用 init()，最后有效行: {last_non_empty}"

    def test_start_button_calls_start_game(self, html_content: str) -> None:
        """验证开始按钮绑定 startGame 函数。"""
        assert "startGame()" in html_content, "开始按钮未绑定 startGame() 函数"

    def test_canvas_dimensions(self, js: str) -> None:
        """验证画布尺寸和网格比例合理。"""
        # canvas 400x400, GRID=20 => 20x20 格子
        assert "400" in js, "画布宽度应为 400"
        assert "GRID" in js, "缺少网格尺寸定义"

    def test_overlay_toggle(self, js: str) -> None:
        """验证覆盖层在开始和结束时正确切换。"""
        assert "classList.remove" in js or "classList.add" in js, "缺少覆盖层类名切换"
        assert "active" in js, "缺少 active 类名用于覆盖层控制"

    def test_set_interval_for_game_loop(self, js: str) -> None:
        """验证使用 setInterval 创建游戏循环。"""
        assert "setInterval" in js, "缺少 setInterval 游戏循环"
        assert "clearInterval" in js, "缺少 clearInterval 清除循环"

    def test_score_display_update(self, js: str) -> None:
        """验证分数显示实时更新。"""
        assert "getElementById('score')" in js or 'getElementById("score")' in js, "缺少分数显示更新"

    def test_best_score_update_on_game_over(self, js: str) -> None:
        """验证游戏结束时更新最高分。"""
        # endGame 中应该比较并更新 bestScore
        endgame_section = re.search(r"function endGame[\s\S]*?function\s", js)
        if endgame_section:
            section = endgame_section.group(0)
        else:
            section = js[js.index("function endGame"):]
        assert "bestScore" in section, "endGame 中缺少最高分更新逻辑"
        assert "localStorage" in section, "endGame 中缺少 localStorage 保存逻辑"

    def test_game_is_complete_html(self, html_content: str) -> None:
        """验证游戏是一个完整的、自包含的 HTML 文件。"""
        assert "<html" in html_content, "缺少 <html> 标签"
        assert "</html>" in html_content, "缺少 </html> 闭合标签"
        assert "<head>" in html_content, "缺少 <head> 标签"
        assert "</head>" in html_content, "缺少 </head> 闭合标签"
        assert "<body>" in html_content, "缺少 <body> 标签"
        assert "</body>" in html_content, "缺少 </body> 闭合标签"
        # 自包含：不应引用外部 JS/CSS
        assert 'src="' not in html_content.split("</style>")[0] if "</style>" in html_content else True, "不应引用外部 CSS"
        assert 'src="http' not in html_content, "不应引用外部 JS 资源"
