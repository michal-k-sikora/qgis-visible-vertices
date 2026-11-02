"""Batched, read-only layer decorations in canvas pixel coordinates."""

import math

from qgis.gui import QgsMapCanvasItem
from qgis.PyQt.QtCore import QPointF, QRectF, Qt
from qgis.PyQt.QtGui import QColor, QFont, QPainter, QPainterPath, QPen

ARROW_SPACING = 80.0  # logical pixels, independent of map CRS and display DPI
MAX_DECORATIONS = 10000
MAX_SCANNED_VERTICES = 250000


def segment_window(a, b, width, height, margin=10):
    """Liang-Barsky clipping, preserving the original segment direction."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    lo, hi = 0.0, 1.0
    for p, q in ((-dx, a[0] + margin), (dx, width + margin - a[0]),
                 (-dy, a[1] + margin), (dy, height + margin - a[1])):
        if p == 0:
            if q < 0:
                return None
        elif p < 0:
            lo = max(lo, q / p)
        else:
            hi = min(hi, q / p)
        if lo > hi:
            return None
    return lo, hi


def line_arrows(points, width, height, limit=MAX_DECORATIONS):
    """Sample local tangents by screen arc length; never join separate parts.

    Clip segments before sampling so a very long offscreen segment costs O(1).
    A short part still receives one arrow at its midpoint.
    """
    distance = 0.0
    visible_midpoint = None
    count = 0
    for a, b in zip(points, points[1:]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        if not length or not math.isfinite(length):
            continue
        window = segment_window(a, b, width, height)
        if window is not None:
            start, end = window[0] * length, window[1] * length
            middle = (start + end) / 2
            visible_midpoint = (a[0] + dx * middle / length,
                                a[1] + dy * middle / length, dx / length, dy / length)
            first = max(0, math.ceil((distance + start - ARROW_SPACING / 2) / ARROW_SPACING))
            offset = ARROW_SPACING / 2 + first * ARROW_SPACING - distance
            while offset <= end and count < limit:
                yield (a[0] + dx * offset / length, a[1] + dy * offset / length,
                       dx / length, dy / length)
                count += 1
                offset += ARROW_SPACING
        distance += length
        if count >= limit:
            return
    if count == 0 and visible_midpoint is not None and limit > 0:
        yield visible_midpoint


class GeometryDecorations(QgsMapCanvasItem):
    """One item and two painter paths, rather than one GUI object per label."""

    def __init__(self, canvas):
        self._canvas = canvas
        self.arrows = []
        self.labels = []
        self.label_texts = []
        self._arrow_path = QPainterPath()
        self._label_path = QPainterPath()
        super().__init__(canvas)
        self.setZValue(1000)
        self.hide()

    def boundingRect(self):
        return QRectF(0, 0, self._canvas.width(), self._canvas.height())

    def updatePosition(self):
        # Data are already in pixels. Hide stale decorations until the debounce fires.
        self.hide()
        self.prepareGeometryChange()
        self.setPos(0, 0)

    def clear(self):
        self.arrows = []
        self.labels = []
        self.label_texts = []
        self.hide()

    def set_data(self, arrows, labels, marker_size):
        self.prepareGeometryChange()
        self.setPos(0, 0)
        self.arrows, self.labels = arrows, labels
        path = QPainterPath()
        for x, y, dx, dy in arrows:
            path.moveTo(x - 5 * dx + 4 * dy, y - 5 * dy - 4 * dx)
            path.lineTo(x + 5 * dx, y + 5 * dy)
            path.lineTo(x - 5 * dx - 4 * dy, y - 5 * dy + 4 * dx)
        self._arrow_path = path
        font = QFont()
        font.setPixelSize(12)
        font.setBold(True)
        path = QPainterPath()
        # Font outlines can contain overlapping subpaths. Odd-even filling
        # cancels their intersection, producing broken counters (notably '0').
        path.setFillRule(Qt.FillRule.WindingFill)
        offset = marker_size / 2 + 4
        grouped = {}
        for x, y, index in labels:
            key = (round(x, 1), round(y, 1))
            grouped.setdefault(key, set()).add(index)
        # A closing vertex shares its position with vertex 0. Show both indices
        # together, instead of painting two glyphs on top of each other.
        self.label_texts = []
        for (x, y), indices in grouped.items():
            text = ", ".join(str(index) for index in sorted(indices))
            self.label_texts.append((x, y, text))
            path.addText(QPointF(x + offset, y - offset), font, text)
        self._label_path = path
        self.setVisible(bool(arrows or labels))
        self.update()

    def paint(self, painter, *_):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setClipRect(self.boundingRect())
        painter.setBrush(Qt.BrushStyle.NoBrush)
        # White casing keeps arrows and labels legible over light and dark maps.
        painter.setPen(QPen(QColor('white'), 4.5))
        painter.drawPath(self._arrow_path)
        painter.setPen(QPen(QColor('#006b8f'), 2.0))
        painter.drawPath(self._arrow_path)
        painter.strokePath(self._label_path, QPen(QColor('white'), 3.0))
        painter.fillPath(self._label_path, QColor('#202020'))
        painter.restore()
