"""stress2_linked_list 双向链表单元测试。"""

from __future__ import annotations

import pytest

from stress2_linked_list import DoublyLinkedList


class TestDoublyLinkedListInsert:
    """测试 insert 方法。"""

    def test_insert_to_empty_list(self) -> None:
        """向空链表插入元素后，链表非空且 to_list 返回正确结果。"""
        dll = DoublyLinkedList()
        dll.insert(1)
        assert dll.to_list() == [1]

    def test_insert_multiple_values(self) -> None:
        """连续插入多个值，按插入顺序排列。"""
        dll = DoublyLinkedList()
        dll.insert(1)
        dll.insert(2)
        dll.insert(3)
        assert dll.to_list() == [1, 2, 3]

    def test_insert_duplicate_values(self) -> None:
        """允许插入重复值。"""
        dll = DoublyLinkedList()
        dll.insert(1)
        dll.insert(1)
        assert dll.to_list() == [1, 1]

    def test_insert_various_types(self) -> None:
        """支持插入不同类型的值（字符串、None 等）。"""
        dll = DoublyLinkedList()
        dll.insert("hello")
        dll.insert(0)
        dll.insert(None)
        assert dll.to_list() == ["hello", 0, None]


class TestDoublyLinkedListSearch:
    """测试 search 方法。"""

    def test_search_existing_value(self) -> None:
        """搜索存在的值返回 True。"""
        dll = DoublyLinkedList()
        dll.insert(10)
        dll.insert(20)
        assert dll.search(10) is True
        assert dll.search(20) is True

    def test_search_non_existing_value(self) -> None:
        """搜索不存在的值返回 False。"""
        dll = DoublyLinkedList()
        dll.insert(1)
        assert dll.search(999) is False

    def test_search_empty_list(self) -> None:
        """空链表中搜索返回 False。"""
        dll = DoublyLinkedList()
        assert dll.search(1) is False

    def test_search_returns_bool(self) -> None:
        """search 返回值为 bool 类型。"""
        dll = DoublyLinkedList()
        dll.insert(5)
        result = dll.search(5)
        assert isinstance(result, bool)


class TestDoublyLinkedListDelete:
    """测试 delete 方法。"""

    def test_delete_existing_value(self) -> None:
        """删除存在的值后，该值不再出现在链表中。"""
        dll = DoublyLinkedList()
        dll.insert(1)
        dll.insert(2)
        dll.insert(3)
        result = dll.delete(2)
        assert result is True
        assert dll.to_list() == [1, 3]
        assert dll.search(2) is False

    def test_delete_head(self) -> None:
        """删除头节点。"""
        dll = DoublyLinkedList()
        dll.insert(1)
        dll.insert(2)
        result = dll.delete(1)
        assert result is True
        assert dll.to_list() == [2]

    def test_delete_tail(self) -> None:
        """删除尾节点。"""
        dll = DoublyLinkedList()
        dll.insert(1)
        dll.insert(2)
        result = dll.delete(2)
        assert result is True
        assert dll.to_list() == [1]

    def test_delete_only_node(self) -> None:
        """删除链表中唯一的节点，链表变为空。"""
        dll = DoublyLinkedList()
        dll.insert(42)
        result = dll.delete(42)
        assert result is True
        assert dll.to_list() == []

    def test_delete_non_existing_value(self) -> None:
        """删除不存在的值返回 False，链表不变。"""
        dll = DoublyLinkedList()
        dll.insert(1)
        dll.insert(2)
        result = dll.delete(99)
        assert result is False
        assert dll.to_list() == [1, 2]

    def test_delete_from_empty_list(self) -> None:
        """从空链表删除返回 False。"""
        dll = DoublyLinkedList()
        result = dll.delete(1)
        assert result is False

    def test_delete_duplicate_only_removes_first(self) -> None:
        """有重复值时，delete 只删除第一个匹配。"""
        dll = DoublyLinkedList()
        dll.insert(1)
        dll.insert(2)
        dll.insert(1)
        dll.delete(1)
        assert dll.to_list() == [2, 1]


class TestDoublyLinkedListReverse:
    """测试 reverse 方法。"""

    def test_reverse_multiple_elements(self) -> None:
        """反转多个元素的链表。"""
        dll = DoublyLinkedList()
        dll.insert(1)
        dll.insert(2)
        dll.insert(3)
        dll.reverse()
        assert dll.to_list() == [3, 2, 1]

    def test_reverse_empty_list(self) -> None:
        """反转空链表不报错。"""
        dll = DoublyLinkedList()
        dll.reverse()
        assert dll.to_list() == []

    def test_reverse_single_element(self) -> None:
        """反转单元素链表，结果不变。"""
        dll = DoublyLinkedList()
        dll.insert(1)
        dll.reverse()
        assert dll.to_list() == [1]

    def test_reverse_two_elements(self) -> None:
        """反转两个元素的链表。"""
        dll = DoublyLinkedList()
        dll.insert(1)
        dll.insert(2)
        dll.reverse()
        assert dll.to_list() == [2, 1]

    def test_reverse_then_insert(self) -> None:
        """反转后继续插入，新元素加在尾部。"""
        dll = DoublyLinkedList()
        dll.insert(1)
        dll.insert(2)
        dll.reverse()
        dll.insert(3)
        assert dll.to_list() == [2, 1, 3]

    def test_double_reverse_restores(self) -> None:
        """两次反转恢复原始顺序。"""
        dll = DoublyLinkedList()
        dll.insert(1)
        dll.insert(2)
        dll.insert(3)
        dll.reverse()
        dll.reverse()
        assert dll.to_list() == [1, 2, 3]


class TestDoublyLinkedListToList:
    """测试 to_list 方法。"""

    def test_empty_list(self) -> None:
        """空链表返回空列表。"""
        dll = DoublyLinkedList()
        assert dll.to_list() == []

    def test_non_empty_list(self) -> None:
        """非空链表返回正确的列表。"""
        dll = DoublyLinkedList()
        dll.insert(10)
        dll.insert(20)
        assert dll.to_list() == [10, 20]

    def test_returns_new_list(self) -> None:
        """to_list 返回新列表，修改不影响链表内部。"""
        dll = DoublyLinkedList()
        dll.insert(1)
        result = dll.to_list()
        result.append(999)
        assert dll.to_list() == [1]
