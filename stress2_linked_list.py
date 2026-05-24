"""双向链表数据结构实现。"""

from __future__ import annotations


class _Node:
    """链表节点（内部实现）。"""

    __slots__ = ("value", "prev", "next")

    def __init__(self, value: object = None) -> None:
        self.value: object = value
        self.prev: _Node | None = None
        self.next: _Node | None = None


class DoublyLinkedList:
    """双向链表。

    支持 insert（尾部插入）、delete（按值删除首个匹配）、
    search（按值查找）、reverse（原地反转）、to_list（转为列表）。
    """

    def __init__(self) -> None:
        self._head: _Node | None = None
        self._tail: _Node | None = None

    def insert(self, value: object) -> None:
        """在链表尾部插入一个值。

        Args:
            value: 要插入的值，支持任意类型。
        """
        new_node = _Node(value)
        if self._tail is None:
            # 空链表：头尾均指向新节点
            self._head = new_node
            self._tail = new_node
        else:
            # 非空链表：追加到尾部
            new_node.prev = self._tail
            self._tail.next = new_node
            self._tail = new_node

    def delete(self, value: object) -> bool:
        """删除链表中第一个匹配指定值的节点。

        Args:
            value: 要删除的值。

        Returns:
            成功删除返回 True，未找到返回 False。
        """
        current = self._head
        while current is not None:
            if current.value == value:
                # 更新前驱的 next 指针
                if current.prev is not None:
                    current.prev.next = current.next
                else:
                    self._head = current.next

                # 更新后继的 prev 指针
                if current.next is not None:
                    current.next.prev = current.prev
                else:
                    self._tail = current.prev

                return True
            current = current.next
        return False

    def search(self, value: object) -> bool:
        """搜索链表中是否存在指定值。

        Args:
            value: 要查找的值。

        Returns:
            存在返回 True，不存在返回 False。
        """
        current = self._head
        while current is not None:
            if current.value == value:
                return True
            current = current.next
        return False

    def reverse(self) -> None:
        """原地反转链表。"""
        current = self._head
        # 交换每个节点的 prev 和 next
        while current is not None:
            current.prev, current.next = current.next, current.prev
            # 反转后，原 next 已变成 prev，顺着原 prev（即新 next）方向已无意义，
            # 应沿着反转前的 prev（现存在 current.next）继续——
            # 但我们刚交换了，所以 current.prev 是原来的 next。
            current = current.prev  # 反转前是 next

        # 交换头尾指针
        self._head, self._tail = self._tail, self._head

    def to_list(self) -> list[object]:
        """将链表转为 Python 列表。

        Returns:
            按从头到尾顺序排列的列表。
        """
        result: list[object] = []
        current = self._head
        while current is not None:
            result.append(current.value)
            current = current.next
        return result
