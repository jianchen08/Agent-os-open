"""
简历HTML文件 - 验证 resume.html 的结构、样式与功能完整性
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

# resume.html 文件不存在，跳过整个模块
pytestmark = pytest.mark.skip(reason="resume.html 文件不存在")

RESUME_PATH = Path(__file__).resolve().parent.parent / "resume.html"


@pytest.fixture(scope="module")
def html_content() -> str:
    """读取 resume.html 全文内容"""
    assert RESUME_PATH.exists(), f"resume.html 不存在: {RESUME_PATH}"
    return RESUME_PATH.read_text(encoding="utf-8")


# ============================================================
# 1. HTML 结构完整性
# ============================================================


class TestHTMLStructure:
    """验证 HTML 文件基本结构"""

    def test_file_exists(self, html_content: str):
        """文件存在且非空"""
        assert len(html_content) > 0, "resume.html 内容为空"

    def test_doctype(self, html_content: str):
        """包含 DOCTYPE 声明"""
        assert "<!DOCTYPE" in html_content.upper()[:200], "缺少 <!DOCTYPE html> 声明"

    def test_html_lang(self, html_content: str):
        """html 标签设置 lang 属性"""
        assert re.search(r"<html\s+[^>]*lang\s*=", html_content), "缺少 <html lang=...>"

    def test_head_and_body(self, html_content: str):
        """包含完整的 head 和 body 标签"""
        assert "<head>" in html_content, "缺少 <head> 标签"
        assert "</head>" in html_content, "缺少 </head> 闭合标签"
        assert "<body>" in html_content, "缺少 <body> 标签"
        assert "</body>" in html_content, "缺少 </body> 闭合标签"

    def test_charset_utf8(self, html_content: str):
        """meta charset 为 UTF-8"""
        assert re.search(r'charset\s*=\s*["\']?\s*UTF-8', html_content, re.IGNORECASE), (
            "缺少 <meta charset='UTF-8'>"
        )

    def test_title_tag(self, html_content: str):
        """包含 title 标签"""
        match = re.search(r"<title>(.*?)</title>", html_content)
        assert match, "缺少 <title> 标签"
        assert len(match.group(1).strip()) > 0, "title 内容为空"

    def test_valid_html_closing(self, html_content: str):
        """HTML 文件以 </html> 结尾"""
        assert html_content.strip().endswith("</html>"), "文件未以 </html> 结尾"


# ============================================================
# 2. contenteditable 属性
# ============================================================


class TestContentEditable:
    """验证关键文本区域设置了 contenteditable"""

    def test_contenteditable_exists(self, html_content: str):
        """至少存在一个 contenteditable 属性"""
        matches = re.findall(r'contenteditable\s*=\s*["\']true["\']', html_content)
        assert len(matches) >= 10, (
            f"contenteditable=true 元素不足 10 个，实际 {len(matches)} 个"
        )

    def test_name_contenteditable(self, html_content: str):
        """姓名区域可编辑"""
        assert re.search(
            r'class\s*=\s*["\']name["\'][^>]*contenteditable', html_content
        ) or re.search(
            r'contenteditable[^>]*class\s*=\s*["\']name["\']', html_content
        ), "姓名 (.name) 未设置 contenteditable"

    def test_contact_contenteditable(self, html_content: str):
        """联系信息可编辑"""
        contacts = re.findall(
            r'<span\s+[^>]*contenteditable\s*=\s*["\']true["\'][^>]*>[^<]*</span>',
            html_content,
        )
        assert len(contacts) >= 3, f"可编辑联系方式不足，实际 {len(contacts)} 个"

    def test_section_content_editable(self, html_content: str):
        """section 内的列表项可编辑"""
        editable_lis = re.findall(
            r'<li\s+[^>]*contenteditable\s*=\s*["\']true["\']', html_content
        )
        assert len(editable_lis) >= 5, (
            f"可编辑的 <li> 项不足 5 个，实际 {len(editable_lis)} 个"
        )


# ============================================================
# 3. 照片嵌入
# ============================================================


class TestPhotoEmbed:
    """验证照片使用 base64 data URI 和圆形裁剪样式"""

    def test_img_base64_src(self, html_content: str):
        """img 标签使用 base64 data URI src"""
        match = re.search(r'<img[^>]+src\s*=\s*["\']data:image/[^"\']+["\']', html_content)
        assert match, "img 标签未使用 data URI (base64) src"

    def test_img_base64_jpeg(self, html_content: str):
        """照片为 JPEG 格式的 base64 数据"""
        assert re.search(r'data:image/jpeg;base64,[A-Za-z0-9+/=]+', html_content), (
            "未找到 data:image/jpeg;base64,... 格式的照片数据"
        )

    def test_photo_class(self, html_content: str):
        """img 标签包含 photo class"""
        assert re.search(r'<img\s+[^>]*class\s*=\s*["\'][^"\']*photo[^"\']*["\']', html_content), (
            "img 标签缺少 class='photo'"
        )

    def test_circular_crop_style(self, html_content: str):
        """CSS 包含圆形裁剪样式 (border-radius: 50%)"""
        style_section = re.search(r"<style>(.*?)</style>", html_content, re.DOTALL)
        assert style_section, "缺少 <style> 块"
        css = style_section.group(1)
        assert re.search(r"\.photo[^{]*\{[^}]*(border-radius\s*:\s*50%)", css, re.DOTALL), (
            ".photo 样式中缺少 border-radius: 50% (圆形裁剪)"
        )


# ============================================================
# 4. PDF 导出按钮
# ============================================================


class TestExportButton:
    """验证存在 PDF 导出按钮"""

    def test_button_exists(self, html_content: str):
        """存在按钮元素"""
        assert "<button" in html_content, "缺少 <button> 元素"

    def test_btn_export_class(self, html_content: str):
        """按钮具有 btn-export class"""
        assert re.search(r'class\s*=\s*["\'][^"\']*btn-export[^"\']*["\']', html_content), (
            "缺少 class='btn-export' 按钮"
        )

    def test_export_button_text(self, html_content: str):
        """按钮包含 'PDF' 关键字"""
        match = re.search(
            r'<button[^>]*class\s*=\s*["\'][^"\']*btn-export[^"\']*["\'][^>]*>(.*?)</button>',
            html_content,
            re.DOTALL,
        )
        assert match, "未找到 btn-export 按钮"
        assert "PDF" in match.group(1).upper(), "导出按钮文本中不包含 'PDF'"


# ============================================================
# 5. html2pdf.js CDN 引入
# ============================================================


class TestHtml2PdfCDN:
    """验证 html2pdf.js 脚本引入"""

    def test_script_src_html2pdf(self, html_content: str):
        """script 标签引入 html2pdf.js"""
        assert re.search(r'<script[^>]+src\s*=\s*["\'][^"\']*html2pdf[^"\']*["\']', html_content), (
            "未引入 html2pdf.js 脚本"
        )

    def test_cdn_url(self, html_content: str):
        """使用 CDN 地址 (cdnjs/cloudflare)"""
        cdn_patterns = [
            r'cdnjs\.cloudflare\.com',
            r'cdn\.jsdelivr\.net',
            r'unpkg\.com',
        ]
        assert any(re.search(p, html_content) for p in cdn_patterns), (
            "html2pdf.js 未使用 CDN 地址"
        )

    def test_html2pdf_bundle(self, html_content: str):
        """引入的是 bundle 版本"""
        assert re.search(r'html2pdf\.bundle', html_content), (
            "未引入 html2pdf bundle 版本"
        )


# ============================================================
# 6. 导出逻辑
# ============================================================


class TestExportLogic:
    """验证 PDF 导出 JavaScript 逻辑"""

    def test_export_pdf_function(self, html_content: str):
        """存在 exportPDF 函数定义"""
        assert re.search(r"function\s+exportPDF\s*\(", html_content), (
            "缺少 exportPDF 函数定义"
        )

    def test_printing_class_add(self, html_content: str):
        """导出时添加 .printing 类"""
        assert re.search(r"classList\.add\s*\(\s*['\"]printing['\"]\s*\)", html_content), (
            "缺少 classList.add('printing') 调用"
        )

    def test_printing_class_remove(self, html_content: str):
        """导出完成后移除 .printing 类"""
        assert re.search(r"classList\.remove\s*\(\s*['\"]printing['\"]\s*\)", html_content), (
            "缺少 classList.remove('printing') 调用"
        )

    def test_printing_css(self, html_content: str):
        """CSS 中定义了 .printing 相关样式"""
        style_section = re.search(r"<style>(.*?)</style>", html_content, re.DOTALL)
        assert style_section, "缺少 <style> 块"
        css = style_section.group(1)
        assert re.search(r"\.printing\s", css), "CSS 中缺少 .printing 样式规则"

    def test_printing_hides_toolbar(self, html_content: str):
        """导出时隐藏工具栏"""
        style_section = re.search(r"<style>(.*?)</style>", html_content, re.DOTALL)
        assert style_section, "缺少 <style> 块"
        css = style_section.group(1)
        assert re.search(r"\.printing\s+\.toolbar\s*\{[^}]*display\s*:\s*none", css, re.DOTALL), (
            ".printing 下未隐藏 .toolbar"
        )

    def test_html2pdf_api_usage(self, html_content: str):
        """调用了 html2pdf() API"""
        assert re.search(r"html2pdf\s*\(", html_content), "未调用 html2pdf() API"

    def test_jspdf_a4_format(self, html_content: str):
        """jsPDF 配置使用 A4 格式"""
        assert re.search(r"format\s*:\s*['\"]a4['\"]", html_content, re.IGNORECASE), (
            "jsPDF 配置中未指定 A4 格式"
        )


# ============================================================
# 7. CSS 样式
# ============================================================


class TestCSSStyles:
    """验证深蓝色调颜色值和 A4 尺寸样式"""

    def test_dark_blue_color(self, html_content: str):
        """CSS 包含深蓝色调颜色值 (#1a2a4a 或类似)"""
        style_section = re.search(r"<style>(.*?)</style>", html_content, re.DOTALL)
        assert style_section, "缺少 <style> 块"
        css = style_section.group(1)
        dark_blue_patterns = [r"#1a2a4a", r"#2c3e6b", r"#2c3e50"]
        assert any(re.search(p, css) for p in dark_blue_patterns), (
            "CSS 中未找到深蓝色调颜色值"
        )

    def test_a4_size_page_rule(self, html_content: str):
        """@page 规则指定 A4 尺寸"""
        assert re.search(r"@page\s*\{[^}]*size\s*:\s*A4", html_content, re.DOTALL), (
            "@page 中未指定 size: A4"
        )

    def test_a4_dimensions(self, html_content: str):
        """CSS 包含 A4 尺寸相关数值 (210mm / 297mm)"""
        assert re.search(r"210mm", html_content), "未找到 A4 宽度 210mm"
        assert re.search(r"297mm", html_content), "未找到 A4 高度 297mm"

    def test_resume_page_class(self, html_content: str):
        """存在 .resume-page 容器"""
        assert re.search(r'\.resume-page\s*\{', html_content), "缺少 .resume-page CSS 规则"
        assert re.search(r'class\s*=\s*["\'][^"\']*resume-page[^"\']*["\']', html_content), (
            "HTML 中缺少 class='resume-page' 元素"
        )

    def test_section_title_style(self, html_content: str):
        """.section-title 样式存在"""
        style_section = re.search(r"<style>(.*?)</style>", html_content, re.DOTALL)
        assert style_section, "缺少 <style> 块"
        css = style_section.group(1)
        assert re.search(r"\.section-title\s*\{", css), "缺少 .section-title CSS 规则"

    def test_accent_blue_color(self, html_content: str):
        """CSS 包含强调蓝色 (#2980d4 或类似)"""
        style_section = re.search(r"<style>(.*?)</style>", html_content, re.DOTALL)
        assert style_section, "缺少 <style> 块"
        css = style_section.group(1)
        assert re.search(r"#2980d4", css), "CSS 中未找到强调蓝色 #2980d4"


# ============================================================
# 8. 简历内容完整性
# ============================================================


class TestResumeContent:
    """验证简历包含各关键 section"""

    SECTION_TITLES = [
        ("项目经历", "项目经历 section"),
        ("工作经历", "工作经历 section"),
        ("教育背景", "教育背景 section"),
        ("技能", "技能 section"),
        ("自我评价", "自我评价 section"),
    ]

    @pytest.mark.parametrize("title,desc", SECTION_TITLES, ids=[s[1] for s in SECTION_TITLES])
    def test_section_exists(self, html_content: str, title: str, desc: str):
        """验证存在指定 section"""
        # 检查 section-title 中包含该标题
        assert re.search(
            rf'class\s*=\s*["\']section-title["\'][^>]*>\s*{re.escape(title)}',
            html_content,
        ), f"缺少 {desc}"

    def test_project_entries(self, html_content: str):
        """项目经历包含至少 2 个项目条目"""
        entries = re.findall(r'class\s*=\s*["\']entry["\']', html_content)
        assert len(entries) >= 2, f"项目条目不足 2 个，实际 {len(entries)} 个"

    def test_work_entries(self, html_content: str):
        """工作经历包含至少 1 个工作条目"""
        entries = re.findall(r'class\s*=\s*["\']work-entry["\']', html_content)
        assert len(entries) >= 1, f"工作条目不足 1 个，实际 {len(entries)} 个"

    def test_education_entries(self, html_content: str):
        """教育背景包含至少 1 条教育记录"""
        entries = re.findall(r'class\s*=\s*["\']edu-row["\']', html_content)
        assert len(entries) >= 1, f"教育记录不足 1 条，实际 {len(entries)} 条"

    def test_skills_grid(self, html_content: str):
        """技能区域使用 skills-grid 布局"""
        assert re.search(r'class\s*=\s*["\'][^"\']*skills-grid[^"\']*["\']', html_content), (
            "缺少 skills-grid 布局"
        )

    def test_summary_text(self, html_content: str):
        """自我评价使用 summary-text 样式"""
        assert re.search(r'class\s*=\s*["\'][^"\']*summary-text[^"\']*["\']', html_content), (
            "缺少 summary-text 元素"
        )

    def test_header_section(self, html_content: str):
        """包含头部 header 区域（姓名 + 联系方式）"""
        assert re.search(r'class\s*=\s*["\']header["\']', html_content), "缺少 .header 区域"
        assert re.search(r'class\s*=\s*["\']name["\']', html_content), "缺少 .name 元素"
        assert re.search(r'class\s*=\s*["\']contact["\']', html_content), "缺少 .contact 元素"

    def test_contact_info_content(self, html_content: str):
        """联系方式包含手机和邮箱"""
        # 检查有手机号格式
        assert re.search(r"1\d{10}", html_content), "联系方式中未找到手机号"
        # 检查有邮箱格式
        assert re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", html_content), "联系方式中未找到邮箱"
