"""Заглушка PySide6 для дымовой проверки кода интерфейса там, где Qt не установлен.

Любой атрибут/класс/вызов возвращает безопасный «пустой» объект. Это проверяет только
импорты и выполнение конструкторов (NameError, TypeError, неверные вызовы СВОИХ функций),
но НЕ заменяет запуск на настоящем PySide6.
"""
from __future__ import annotations

import sys
import types

class _Meta(type):
    def __getattr__(cls, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _get_class(name)

    def __or__(cls, other):
        return cls

    def __ror__(cls, other):
        return cls


class _Any(metaclass=_Meta):
    def __init__(self, *args, **kwargs):
        pass

    def __getattr__(self, name):
        # У методов и сигналов Qt имена в camelCase без подчёркиваний. Имя с «_» — это атрибут или метод нашего
        # кода: если его нет, это ошибка, а не «метод Qt», поэтому заглушка её не скрывает.
        if name.startswith("__") or "_" in name:
            raise AttributeError(f"{type(self).__name__} has no attribute {name!r}")
        return _Any()

    def __call__(self, *args, **kwargs):
        if len(args) == 1 and callable(args[0]) and not kwargs:
            return args[0]
        return _Any()

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 0

    def __bool__(self):
        return True

    def __index__(self):
        return 0

    def __int__(self):
        return 0

    def __or__(self, other):
        return self

    __ror__ = __or__

    def __getitem__(self, item):
        return self

    def __mul__(self, other):
        return self

    __rmul__ = __add__ = __radd__ = __sub__ = __rsub__ = __truediv__ = __floordiv__ = __mul__

    def __lt__(self, other):
        return False

    __le__ = __gt__ = __ge__ = __lt__

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _LineEdit(_Any):
    def __init__(self, *a, **k):
        self._t = ""

    def text(self):
        return self._t

    def setText(self, t):
        self._t = t

    def clear(self):
        self._t = ""


class _SpinBox(_Any):
    def __init__(self, *a, **k):
        self._v = 0

    def value(self):
        return self._v

    def setValue(self, v):
        self._v = v


class _CheckBox(_Any):
    def __init__(self, *a, **k):
        self._c = False

    def isChecked(self):
        return self._c

    def setChecked(self, c):
        self._c = bool(c)


class _PlainText(_Any):
    def __init__(self, *a, **k):
        self._t = ""

    def toPlainText(self):
        return self._t

    def setPlainText(self, t):
        self._t = t

    def appendPlainText(self, t):
        self._t += ("\n" if self._t else "") + t

    def clear(self):
        self._t = ""


class _ComboBox(_Any):
    def __init__(self, *a, **k):
        self._items = []
        self._i = 0
        self._edit = None

    def setEditable(self, flag):
        pass

    def setEditText(self, t):
        self._edit = t

    def currentText(self):
        if self._edit is not None:
            return self._edit
        return self._items[self._i][0] if self._items else ""

    def itemText(self, i):
        return self._items[i][0] if 0 <= i < len(self._items) else ""

    def itemData(self, i):
        return self._items[i][1] if 0 <= i < len(self._items) else None

    def clear(self):
        self._items, self._i, self._edit = [], 0, None

    def addItem(self, text, data=None):
        self._items.append((text, data))

    def currentData(self):
        return self._items[self._i][1] if self._items else None

    def currentIndex(self):
        return self._i

    def setCurrentIndex(self, i):
        self._i = max(0, min(int(i), len(self._items) - 1)) if self._items else 0

    def findData(self, data):
        for i, (_t, d) in enumerate(self._items):
            if d == data:
                return i
        return -1

    def count(self):
        return len(self._items)


class _TabWidget(_Any):
    def __init__(self, *a, **k):
        self._w = []
        self._i = 0

    def addTab(self, w, title=""):
        self._w.append(w)
        return len(self._w) - 1

    def insertTab(self, i, w, title=""):
        self._w.insert(i, w)
        return i

    def removeTab(self, i):
        del self._w[i]

    def count(self):
        return len(self._w)

    def widget(self, i):
        return self._w[i] if 0 <= i < len(self._w) else None

    def currentWidget(self):
        return self._w[self._i] if self._w else None

    def currentIndex(self):
        return self._i

    def setCurrentIndex(self, i):
        self._i = i

    def setCurrentWidget(self, w):
        if w in self._w:
            self._i = self._w.index(w)

    def indexOf(self, w):
        return self._w.index(w) if w in self._w else -1


_classes: dict[str, type] = {"QPlainTextEdit": _PlainText, "QTabWidget": _TabWidget, "QLineEdit": _LineEdit, "QSpinBox": _SpinBox, "QCheckBox": _CheckBox,
                             "QComboBox": _ComboBox}


def _get_class(name: str) -> type:
    if name not in _classes:
        _classes[name] = type(name, (_Any,), {})
    return _classes[name]


def _module(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__dict__["__getattr__"] = lambda attr: (_ for _ in ()).throw(AttributeError(attr)) \
        if attr.startswith("__") else _get_class(attr)
    mod.__path__ = []  # type: ignore[attr-defined]
    return mod


NAMES = ["PySide6", "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets"]


def install() -> dict:
    saved = {n: sys.modules.get(n) for n in NAMES}
    for n in NAMES:
        sys.modules[n] = _module(n)
    return saved


def uninstall(saved: dict) -> None:
    for n, old in saved.items():
        if old is None:
            sys.modules.pop(n, None)
        else:
            sys.modules[n] = old
    for name in [m for m in sys.modules if m == "ui" or m.startswith("ui.")]:
        sys.modules.pop(name, None)
