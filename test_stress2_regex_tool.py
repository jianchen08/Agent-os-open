"""stress2_regex_tool 的单元测试。"""

from __future__ import annotations

import pytest

from stress2_regex_tool import (
    match_date,
    match_email,
    match_html_tag,
    match_id_card,
    match_ip_address,
    match_phone,
    match_url,
)


class TestMatchEmail:
    """邮箱正则匹配测试。"""

    def test_match_single_email(self) -> None:
        result = match_email("联系我们：admin@example.com")
        assert result == ["admin@example.com"]

    def test_match_multiple_emails(self) -> None:
        text = "发邮件到 a@b.cn 和 test.user@company.co.uk"
        result = match_email(text)
        assert len(result) == 2
        assert "a@b.cn" in result
        assert "test.user@company.co.uk" in result

    def test_no_match(self) -> None:
        assert match_email("这段文字没有邮箱") == []

    def test_invalid_email_not_matched(self) -> None:
        assert match_email("不是邮箱 @example.com") == []


class TestMatchPhone:
    """手机号正则匹配测试（中国大陆11位）。"""

    def test_match_single_phone(self) -> None:
        result = match_phone("手机号：13812345678")
        assert result == ["13812345678"]

    def test_match_multiple_phones(self) -> None:
        text = "电话：13900001111 或 15012349876"
        result = match_phone(text)
        assert len(result) == 2

    def test_no_match(self) -> None:
        assert match_phone("没有手机号") == []

    def test_invalid_phone_not_matched(self) -> None:
        # 不足11位
        assert match_phone("电话：1381234567") == []
        # 非手机号段开头
        assert match_phone("电话：00012345678") == []


class TestMatchUrl:
    """URL 正则匹配测试。"""

    def test_match_http_url(self) -> None:
        result = match_url("访问 https://www.example.com/path?q=1")
        assert "https://www.example.com/path?q=1" in result

    def test_match_ftp_url(self) -> None:
        result = match_url("下载 ftp://files.example.com/archive.zip")
        assert len(result) == 1

    def test_no_match(self) -> None:
        assert match_url("没有URL") == []


class TestMatchIpAddress:
    """IPv4 地址正则匹配测试。"""

    def test_match_single_ip(self) -> None:
        result = match_ip_address("服务器地址是 192.168.1.1")
        assert result == ["192.168.1.1"]

    def test_match_multiple_ips(self) -> None:
        text = "源 10.0.0.1 到目标 172.16.254.1"
        result = match_ip_address(text)
        assert len(result) == 2

    def test_no_match(self) -> None:
        assert match_ip_address("没有IP") == []

    def test_invalid_ip_not_matched(self) -> None:
        # 超出范围 256.1.1.1 不应匹配
        assert match_ip_address("地址 256.1.1.1") == []


class TestMatchIdCard:
    """身份证号正则匹配测试（18位）。"""

    def test_match_id_card(self) -> None:
        result = match_id_card("身份证号：110101199003077731")
        assert result == ["110101199003077731"]

    def test_match_id_card_with_x(self) -> None:
        result = match_id_card("身份证：44010619990101123X")
        assert len(result) == 1

    def test_no_match(self) -> None:
        assert match_id_card("没有身份证号") == []


class TestMatchDate:
    """日期正则匹配测试（YYYY-MM-DD）。"""

    def test_match_date(self) -> None:
        result = match_date("日期是 2024-01-15")
        assert result == ["2024-01-15"]

    def test_match_multiple_dates(self) -> None:
        text = "从 2023-06-01 到 2024-12-31"
        result = match_date(text)
        assert len(result) == 2

    def test_no_match(self) -> None:
        assert match_date("没有日期") == []


class TestMatchHtmlTag:
    """HTML 标签正则匹配测试。"""

    def test_match_html_tag(self) -> None:
        result = match_html_tag("<div class='box'>内容</div>")
        assert "<div class='box'>" in result
        assert "</div>" in result

    def test_match_self_closing_tag(self) -> None:
        result = match_html_tag("图片 <img src='a.jpg' />")
        assert len(result) == 1

    def test_no_match(self) -> None:
        assert match_html_tag("没有HTML标签") == []
