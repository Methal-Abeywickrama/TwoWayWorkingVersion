"""Minimal stand-in for PyQt5 (tests only): every class accepts any call, unknown methods are no-ops, and the few
methods whose results the dashboard / login code computes with return numbers. It finds Python errors in that
code (names, arithmetic, data flow); it does not check how Qt would draw."""


class _Meta(type):
    def __getattr__(cls, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _Any()


class _Any(metaclass=_Meta):
    def __init__(self, *a, **k):
        pass

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _Any()

    def __call__(self, *a, **k):
        return _Any()

    def __or__(self, o):
        return self

    __ror__ = __or__

    def __bool__(self):
        return False

    def __iter__(self):
        return iter(())


class _Flags:
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return 1 << (abs(hash(name)) % 20)


Qt = _Flags()


class QObject(_Any):
    pass


class QWidget(QObject):
    def __init__(self, *a, **k):
        self._w, self._h, self._visible = 600, 230, False

    def width(self):
        return self._w

    def height(self):
        return self._h

    def rect(self):
        return QRectF(0, 0, self._w, self._h)

    def font(self):
        return QFont()

    def show(self):
        self._visible = True
        self.showEvent(None)

    def showEvent(self, e):
        pass

    def hideEvent(self, e):
        pass

    def setVisible(self, v):
        self._visible = bool(v)

    def isVisible(self):
        return self._visible


class QRectF:
    def __init__(self, x=0, y=0, w=0, h=0):
        self._x, self._y, self._w, self._h = float(x), float(y), float(w), float(h)

    def left(self):
        return self._x

    def top(self):
        return self._y

    def right(self):
        return self._x + self._w

    def bottom(self):
        return self._y + self._h

    def width(self):
        return self._w

    def height(self):
        return self._h


class QPointF:
    def __init__(self, x=0, y=0):
        self._x, self._y = float(x), float(y)

    def x(self):
        return self._x

    def y(self):
        return self._y


class QFont(_Any):
    def pointSizeF(self):
        return 10.0


class QFontMetrics(_Any):
    def horizontalAdvance(self, s):
        return 7 * len(str(s))

    def height(self):
        return 14

    def ascent(self):
        return 11


class QComboBox(QWidget):
    def __init__(self, *a, **k):
        super().__init__()
        self._items, self._i = [], 0

    def addItem(self, label, data=None):
        self._items.append((label, data))

    def setCurrentIndex(self, i):
        self._i = i

    def currentData(self):
        return self._items[self._i][1] if self._items else None


class QPlainTextEdit(QWidget):
    def __init__(self, *a, **k):
        super().__init__()
        self._t = ""

    def setPlainText(self, t):
        self._t = t

    def toPlainText(self):
        return self._t


class QLabel(QWidget):
    def __init__(self, text="", *a, **k):
        super().__init__()
        self._t = text

    def setText(self, t):
        self._t = t

    def text(self):
        return self._t


class QSpinBox(QWidget):
    def __init__(self, *a, **k):
        super().__init__()
        self._v = 0

    def setValue(self, v):
        self._v = v

    def value(self):
        return self._v


class QRadioButton(QWidget):
    def __init__(self, *a, **k):
        super().__init__()
        self._c = False

    def setChecked(self, c):
        self._c = bool(c)

    def isChecked(self):
        return self._c
