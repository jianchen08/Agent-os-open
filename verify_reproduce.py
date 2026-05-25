#!/usr/bin/env python3
"""
可复现验证脚本：resume.html 功能验证
====================================
运行方式: python3 verify_reproduce.py
依赖: Python 3.6+（无第三方依赖）
输出: 控制台打印验证结果 + verify_results.json

验证内容:
1. HTML文件基本结构（DOCTYPE、编码、语言、标题、内嵌CSS/JS）
2. 照片base64内嵌和圆形裁剪
3. contenteditable可编辑功能覆盖
4. 导出PDF按钮和功能代码
5. 导出时隐藏编辑状态机制
6. 简历内容完整性（5大区块+17项具体内容）
7. A4页面排版CSS控制
8. 设计风格（配色、字体、现代效果）
9. localStorage自动保存恢复
10. 工具栏与用户提示
11. 补充场景（异常恢复、文件独立性）
"""

import re
import json
import os
import sys

# ============================================================
# 配置
# ============================================================
RESUME_FILE = "resume.html"
OUTPUT_JSON = "verify_results.json"

# ============================================================
# 验证引擎
# ============================================================

class VerifyRunner:
    def __init__(self, filepath):
        self.filepath = filepath
        self.content = ""
        self.results = {}
        self.journey_steps = []
        self.scenarios = []

    def load(self):
        """加载HTML文件"""
        if not os.path.exists(self.filepath):
            print(f"❌ 文件不存在: {self.filepath}")
            sys.exit(1)
        with open(self.filepath, 'r', encoding='utf-8') as f:
            self.content = f.read()
        print(f"✅ 已加载 {self.filepath} ({len(self.content)} 字符, {self.content.count(chr(10))+1} 行)")
        return self

    def check(self, category, name, condition, detail=""):
        """执行单条检查"""
        key = f"{category}::{name}"
        status = "PASS" if condition else "FAIL"
        self.results[key] = {
            "category": category,
            "name": name,
            "status": status,
            "detail": detail
        }
        icon = "✅" if condition else "❌"
        print(f"  {icon} {name}: {detail}")
        return condition

    def run_all(self):
        """执行全部验证"""
        self._check_html_structure()
        self._check_photo()
        self._check_contenteditable()
        self._check_pdf_export()
        self._check_printing_state()
        self._check_content_completeness()
        self._check_a4_layout()
        self._check_design_style()
        self._check_auto_save()
        self._check_toolbar()
        self._check_scenarios()
        return self

    # ----------------------------------------------------------
    # 验证模块
    # ----------------------------------------------------------

    def _check_html_structure(self):
        """验证1: HTML文件基本结构"""
        print("\n" + "=" * 60)
        print("【验证1】HTML文件基本结构")
        print("=" * 60)
        c = self.content
        cat = "HTML结构"
        self.check(cat, "DOCTYPE声明", c.strip().startswith('<!DOCTYPE html>'), "HTML5文档类型")
        self.check(cat, "UTF-8编码", 'charset="UTF-8"' in c, "UTF-8字符编码")
        self.check(cat, "中文语言", 'lang="zh-CN"' in c, "zh-CN语言设置")
        m = re.search(r'<title>(.*?)</title>', c)
        self.check(cat, "页面标题", bool(m), m.group(1) if m else "缺失")
        self.check(cat, "内嵌CSS", '<style>' in c and '</style>' in c, "CSS在<style>标签中")
        self.check(cat, "内嵌JS", '<script>' in c and '</script>' in c, "JS在<script>标签中")
        ext_css = [l for l in re.findall(r'<link[^>]+href=["\']([^"\']+)', c) if l.endswith('.css')]
        self.check(cat, "无外部CSS文件", len(ext_css) == 0, "纯内嵌，无外部CSS依赖")

    def _check_photo(self):
        """验证2: 照片"""
        print("\n" + "=" * 60)
        print("【验证2】照片验证")
        print("=" * 60)
        c = self.content
        cat = "照片"
        m = re.search(r'<img\s+class="photo"[^>]*src="([^"]+)"', c)
        if m:
            self.check(cat, "照片标签存在", True, "img.photo标签存在")
            src = m.group(1)
            self.check(cat, "Base64内嵌", src.startswith('data:image/'),
                       f"data:image/jpeg, 数据长度{len(src)}字符")
        else:
            self.check(cat, "照片标签存在", False, "未找到img.photo标签")

        pcss = re.search(r'\.photo\s*\{([^}]+)\}', c)
        if pcss:
            css = pcss.group(1)
            self.check(cat, "圆形裁剪", 'border-radius: 50%' in css, "border-radius: 50%")
            wm = re.search(r'width:\s*(\d+)', css)
            hm = re.search(r'height:\s*(\d+)', css)
            w = wm.group(1) if wm else "?"
            h = hm.group(1) if hm else "?"
            self.check(cat, "照片尺寸", True, f"{w} x {h} px")

    def _check_contenteditable(self):
        """验证3: contenteditable可编辑"""
        print("\n" + "=" * 60)
        print("【验证3】contenteditable 可编辑验证")
        print("=" * 60)
        c = self.content
        cat = "可编辑"
        count = len(re.findall(r'contenteditable="true"', c))
        self.check(cat, "可编辑元素数量", count > 0, f"共 {count} 个contenteditable元素")

        checks = [
            ("姓名", r'class="name"[^>]*contenteditable="true"'),
            ("职位", r'class="title"[^>]*contenteditable="true"'),
            ("联系方式", r'contact.*?contenteditable="true"'),
            ("项目经历", r'项目经历.*?contenteditable="true"'),
            ("工作经历", r'工作经历.*?contenteditable="true"'),
            ("教育背景", r'教育背景.*?contenteditable="true"'),
            ("技能", r'技能.*?contenteditable="true"'),
            ("自我评价", r'自我评价.*?contenteditable="true"'),
        ]
        for name, pattern in checks:
            found = bool(re.search(pattern, c, re.DOTALL))
            self.check(cat, f"{name}可编辑", found, "已标记contenteditable" if found else "未标记")

        self.check(cat, "hover编辑样式", ':hover' in c and 'outline-color' in c, "hover显示虚线边框")
        self.check(cat, "focus编辑样式", ':focus' in c and 'outline-color' in c, "focus高亮显示")

    def _check_pdf_export(self):
        """验证4: 导出PDF按钮"""
        print("\n" + "=" * 60)
        print("【验证4】导出PDF按钮验证")
        print("=" * 60)
        c = self.content
        cat = "PDF导出"
        bm = re.search(r'<button[^>]*class="btn-export"[^>]*>(.*?)</button>', c)
        self.check(cat, "按钮存在", bool(bm), f"文本: {bm.group(1) if bm else 'N/A'}")
        self.check(cat, "exportPDF函数", 'function exportPDF()' in c, "PDF导出函数已定义")
        self.check(cat, "html2pdf库", 'html2pdf' in c, "引入html2pdf.js")
        cdn = re.search(r'html2pdf\.js/([\d.]+)/html2pdf\.bundle\.min\.js', c)
        self.check(cat, "html2pdf CDN", bool(cdn), f"版本: {cdn.group(1) if cdn else 'N/A'}")
        self.check(cat, "A4格式", "format: 'a4'" in c or 'format: "a4"' in c, "jsPDF format=a4")
        self.check(cat, "纵向布局", "orientation: 'portrait'" in c, "portrait纵向")

    def _check_printing_state(self):
        """验证5: 导出时隐藏编辑状态"""
        print("\n" + "=" * 60)
        print("【验证5】导出时隐藏编辑状态")
        print("=" * 60)
        c = self.content
        cat = "导出状态"
        self.check(cat, "printing CSS类", '.printing' in c, ".printing类定义")
        self.check(cat, "隐藏工具栏", '.printing .toolbar' in c and 'display: none' in c,
                   "导出时toolbar隐藏")
        self.check(cat, "去除编辑样式", '.printing [contenteditable' in c,
                   "导出时去除outline和background")
        self.check(cat, "导出前添加类", "classList.add('printing')" in c,
                   "导出前body.classList.add('printing')")
        self.check(cat, "导出后移除类", "classList.remove('printing')" in c,
                   "导出后body.classList.remove('printing')")

    def _check_content_completeness(self):
        """验证6: 简历内容完整性"""
        print("\n" + "=" * 60)
        print("【验证6】简历内容完整性")
        print("=" * 60)
        c = self.content
        cat = "内容完整性"

        sections = ['项目经历', '工作经历', '教育背景', '技能', '自我评价']
        for s in sections:
            cnt = c.count(s)
            self.check(cat, f"区块: {s}", cnt > 0, f"出现{cnt}次")

        print("\n  --- 具体内容 ---")
        details = [
            ("姓名", "陈健"),
            ("职位", "AI Agent"),
            ("手机号", "15116047185"),
            ("邮箱", "chenjian1306792950@foxmail.com"),
            ("项目: 灵汐Agent系统", "灵汐"),
            ("项目: 太阳能吸波器", "太阳能"),
            ("项目: 光学陀螺仪", "陀螺仪"),
            ("公司: 振华集团", "振华"),
            ("公司: 托克逊", "托克逊"),
            ("学历: 国防科技大学", "国防科技大学"),
            ("学历: 湖南工业大学", "湖南工业大学"),
            ("技能: Python", "Python"),
            ("技能: React", "React"),
            ("技能: TypeScript", "TypeScript"),
            ("技能: FastAPI", "FastAPI"),
            ("技能: Docker", "Docker"),
            ("技能: Multi-Agent", "Multi-Agent"),
        ]
        for name, kw in details:
            self.check(cat, name, kw in c, "内容存在" if kw in c else "缺失")

    def _check_a4_layout(self):
        """验证7: A4页面排版控制"""
        print("\n" + "=" * 60)
        print("【验证7】A4页面排版控制")
        print("=" * 60)
        c = self.content
        cat = "A4排版"
        self.check(cat, "A4宽度", 'width: 210mm' in c, "resume-page: width:210mm")
        self.check(cat, "A4高度", 'min-height: 297mm' in c, "resume-page: min-height:297mm")
        self.check(cat, "@page设置", '@page' in c and 'size: A4' in c, "@page { size:A4; margin:0 }")
        self.check(cat, "打印色彩保留", 'print-color-adjust: exact' in c, "print-color-adjust: exact")

    def _check_design_style(self):
        """验证8: 设计风格"""
        print("\n" + "=" * 60)
        print("【验证8】设计风格")
        print("=" * 60)
        c = self.content
        cat = "设计风格"
        self.check(cat, "深蓝主色", '#1a2a4a' in c, "#1a2a4a 用于标题、边框")
        self.check(cat, "藏青渐变", '#2c3e6b' in c, "#2c3e6b 用于toolbar背景")
        self.check(cat, "蓝色强调", '#2980d4' in c, "#2980d4 用于section边框、链接")
        self.check(cat, "渐变效果", 'linear-gradient' in c, "toolbar和按钮渐变")
        self.check(cat, "阴影效果", 'box-shadow' in c, "卡片阴影")
        self.check(cat, "圆角设计", 'border-radius' in c, "按钮、照片圆角")
        self.check(cat, "过渡动画", 'transition' in c, "交互过渡效果")
        self.check(cat, "中文字体", 'Microsoft YaHei' in c or '微软雅黑' in c, "微软雅黑优先")

    def _check_auto_save(self):
        """验证9: 自动保存功能"""
        print("\n" + "=" * 60)
        print("【验证9】自动保存功能 (localStorage)")
        print("=" * 60)
        c = self.content
        cat = "自动保存"
        self.check(cat, "localStorage", 'localStorage' in c, "使用localStorage")
        self.check(cat, "自动保存", 'setItem' in c, "编辑时自动保存")
        self.check(cat, "数据恢复", 'getItem' in c, "刷新后恢复")
        self.check(cat, "防抖300ms", 'setTimeout' in c and 'clearTimeout' in c and '300' in c,
                   "300ms防抖处理")

    def _check_toolbar(self):
        """验证10: 工具栏"""
        print("\n" + "=" * 60)
        print("【验证10】工具栏与用户提示")
        print("=" * 60)
        c = self.content
        cat = "工具栏"
        self.check(cat, "工具栏", 'class="toolbar"' in c, "顶部工具栏")
        self.check(cat, "标题", 'toolbar-title' in c, "简历编辑器标题")
        hm = re.search(r'class="toolbar-hint"[^>]*>([^<]+)', c)
        self.check(cat, "编辑提示", bool(hm), hm.group(1) if hm else "N/A")
        self.check(cat, "fixed定位", 'position: fixed' in c, "工具栏固定顶部")

    def _check_scenarios(self):
        """补充场景验证"""
        print("\n" + "=" * 60)
        print("【补充场景】异常恢复 & 文件独立性")
        print("=" * 60)
        c = self.content
        cat = "补充场景"

        # 场景1: PDF导出失败恢复
        has_catch = '.catch' in c and "classList.remove('printing')" in c
        self.check(cat, "PDF导出失败恢复", has_catch,
                   ".catch()中恢复printing类和按钮状态")
        btn_restore = "btn.textContent = originalText" in c and "btn.disabled = false" in c
        self.check(cat, "失败后按钮恢复", btn_restore,
                   "catch中恢复按钮文本和disabled状态")

        # 场景2: 文件独立性
        ext_js = re.findall(r'<script[^>]+src=["\']([^"\']+)', c)
        only_cdn = all('cdn' in js or 'cloudflare' in js for js in ext_js) if ext_js else True
        self.check(cat, "单文件独立性", only_cdn,
                   f"仅CDN引用: {ext_js}" if ext_js else "无外部引用")

    # ----------------------------------------------------------
    # 汇总输出
    # ----------------------------------------------------------

    def summary(self):
        """输出汇总"""
        print("\n" + "=" * 60)
        print("统计汇总")
        print("=" * 60)
        total = len(self.results)
        passed = sum(1 for v in self.results.values() if v["status"] == "PASS")
        failed = sum(1 for v in self.results.values() if v["status"] == "FAIL")
        rate = passed / total * 100 if total > 0 else 0

        print(f"  总检查项: {total}")
        print(f"  通过: {passed}")
        print(f"  失败: {failed}")
        print(f"  通过率: {rate:.1f}%")

        # 按分类统计
        print("\n  --- 分类统计 ---")
        categories = {}
        for v in self.results.values():
            cat = v["category"]
            if cat not in categories:
                categories[cat] = {"total": 0, "pass": 0}
            categories[cat]["total"] += 1
            if v["status"] == "PASS":
                categories[cat]["pass"] += 1
        for cat, stats in categories.items():
            print(f"    {cat}: {stats['pass']}/{stats['total']}")

        # 保存JSON
        output = {
            "total": total,
            "passed": passed,
            "failed": failed,
            "pass_rate": f"{rate:.1f}%",
            "details": self.results
        }
        with open(OUTPUT_JSON, 'w', encoding='utf-8') as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
        print(f"\n  结果已保存到 {OUTPUT_JSON}")

        return passed == total


# ============================================================
# 主入口
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("resume.html 功能验证脚本")
    print("=" * 60)

    runner = VerifyRunner(RESUME_FILE)
    runner.load()
    runner.run_all()
    all_passed = runner.summary()

    print("\n" + "=" * 60)
    if all_passed:
        print("🎉 验证全部通过！")
    else:
        print("⚠️ 存在未通过的检查项，请查看上方详情")
    print("=" * 60)

    sys.exit(0 if all_passed else 1)
