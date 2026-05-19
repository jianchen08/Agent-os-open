"""
数据库模型基类（非 ORM 存根）

替换原 SQLAlchemy DeclarativeBase，提供最小兼容接口。
"""


class _FakeMetadata:
    """模拟 SQLAlchemy metadata 对象，用于 create_all / drop_all 兼容调用。"""

    def create_all(self, *args, **kwargs):
        pass

    def drop_all(self, *args, **kwargs):
        pass


class Base:
    """模型基类（非 ORM）

    替代 SQLAlchemy DeclarativeBase，所有模型继承此类以保持类型兼容。
    """

    metadata = _FakeMetadata()

    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)

    def __repr__(self):
        attrs = ", ".join(f"{k}={v!r}" for k, v in self.__dict__.items())
        return f"{self.__class__.__name__}({attrs})"
