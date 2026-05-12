"""
数据库模型基类

基于 SQLAlchemy 2.0 的异步模型
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """模型基类"""
