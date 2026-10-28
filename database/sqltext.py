"""Безопасное оформление идентификаторов и типов T-SQL."""
from __future__ import annotations


def quote_ident(name: str) -> str:
    """[имя] с экранированием ']' — единственный способ вставлять имена в SQL."""
    if not isinstance(name, str) or not name or len(name) > 128 or "\x00" in name:
        raise ValueError("Недопустимое имя объекта")
    return "[" + name.replace("]", "]]") + "]"


def qualified_name(*parts: str) -> str:
    return ".".join(quote_ident(p) for p in parts if p)


def format_type(name: str, max_length: int, precision: int, scale: int) -> str:
    n = name.lower()
    if n in ("nvarchar", "nchar"):
        return f"{name}(max)" if max_length == -1 else f"{name}({max_length // 2})"
    if n in ("varchar", "char", "varbinary", "binary"):
        return f"{name}(max)" if max_length == -1 else f"{name}({max_length})"
    if n in ("decimal", "numeric"):
        return f"{name}({precision},{scale})"
    if n in ("datetime2", "time", "datetimeoffset"):
        return f"{name}({scale})"
    if n == "float" and precision not in (0, 53):
        return f"{name}({precision})"
    return name
