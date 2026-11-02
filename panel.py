"""Configuration panel used both in the toolbar popup and in the dock.

Every panel is only a view of VerticesState (and of the native snapping
configuration): it writes user changes there and redraws its rows from the
state signals, so any number of panels stay in sync.
"""

import os

from qgis.core import Qgis, QgsApplication, QgsIconUtils, QgsMapLayerModel, QgsMapLayerProxyModel
from qgis.gui import QgsColorButton, QgsFilterLineEdit
from qgis.PyQt.QtCore import QPointF, QRectF, QSize, Qt, QTimer
from qgis.PyQt.QtGui import QColor, QIcon, QPainter, QPixmap, QPolygonF
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QSizePolicy,
    QSpinBox,
    QStyledItemDelegate,
    QToolButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from . import snapping
from .overlay import MAX_VERTICES_PER_LAYER
from .state import MARKER_SIZE_MAX, MARKER_SIZE_MIN, SHAPES, tr

ROW_MARGIN = 4
ROW_SPACING = 4
COLOR_BUTTON_WIDTH = 44

SHAPE_LABELS = {
    "circle": "Circle",
    "square": "Square",
    "diamond": "Diamond",
    "cross": "Cross",
    "x": "X",
}


def shape_icon(shape, color, size=16):
    """Preview of a marker: outline in the layer color, transparent fill."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = painter.pen()
    pen.setColor(color)
    pen.setWidthF(2.0)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    c = size / 2.0
    r = size * 0.32
    if shape == "cross":
        painter.drawLine(QPointF(c - r, c), QPointF(c + r, c))
        painter.drawLine(QPointF(c, c - r), QPointF(c, c + r))
    elif shape == "x":
        painter.drawLine(QPointF(c - r, c - r), QPointF(c + r, c + r))
        painter.drawLine(QPointF(c - r, c + r), QPointF(c + r, c - r))
    elif shape == "circle":
        painter.drawEllipse(QPointF(c, c), r, r)
    elif shape == "square":
        painter.drawRect(QRectF(c - r, c - r, 2 * r, 2 * r))
    else:
        painter.drawPolygon(QPolygonF([QPointF(c, c - r - 1), QPointF(c + r + 1, c),
                                       QPointF(c, c + r + 1), QPointF(c - r - 1, c)]))
    painter.end()
    return QIcon(pixmap)


def magnet_icon():
    """Magnet icon that is greyed out while the button is unchecked."""
    magnet = QgsApplication.getThemeIcon("/mIconSnapping.svg")
    size = QSize(32, 32)
    icon = QIcon()
    icon.addPixmap(magnet.pixmap(size), QIcon.Mode.Normal, QIcon.State.On)
    icon.addPixmap(magnet.pixmap(size, QIcon.Mode.Disabled), QIcon.Mode.Normal, QIcon.State.Off)
    return icon


class _ElidedLabel(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._full_text = ""
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(40)

    def set_full_text(self, text):
        self._full_text = text
        self.setToolTip(text)
        self._update_text()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_text()

    def _update_text(self):
        self.setText(self.fontMetrics().elidedText(self._full_text, Qt.TextElideMode.ElideRight, self.width()))


class LayerRow(QWidget):
    """[show vertices] [icon] [name] [status] [color] [shape] [selected only] [snapping] for one layer."""

    def __init__(self, panel, layer, parent=None):
        super().__init__(parent)
        self._panel = panel
        self._state = panel.state
        self.layer_id = layer.id()

        self._visible = QCheckBox()
        self._visible.setToolTip(tr("Show the vertices of this layer"))
        self._layer_icon = QLabel()
        self._layer_icon.setPixmap(QgsIconUtils.iconForLayer(layer).pixmap(16, 16))
        self._name = _ElidedLabel()
        self._warning = QLabel()
        self._warning.setPixmap(QgsApplication.getThemeIcon("/mIconWarning.svg").pixmap(16, 16))
        self._warning.setToolTip(
            tr("More than {:,} vertices in the current view; zoom in to display them.").format(MAX_VERTICES_PER_LAYER))
        self._no_selection = QLabel()
        self._no_selection.setPixmap(QgsApplication.getThemeIcon("/mIconInfo.svg").pixmap(16, 16))
        self._no_selection.setToolTip(tr("No features are selected; vertices, indices and directions are not displayed."))
        self._color = QgsColorButton()
        self._color.setColorDialogTitle(tr("Vertex Color"))
        self._color.setFixedWidth(panel.color_width)
        self._shape = QComboBox()
        self._shape.setToolTip(tr("Marker shape"))
        for shape in SHAPES:
            self._shape.addItem(tr(SHAPE_LABELS[shape]), shape)
        self._shape.setFixedWidth(panel.shape_width)
        self._selected = QCheckBox()
        self._selected.setToolTip(tr("Selected features only — applies to vertices, indices and line directions"))
        self._selected.setFixedWidth(panel.selected_width)
        self._numbers = QCheckBox()
        self._numbers.setToolTip(tr("Show vertex indices — actual zero-based vertex numbers"))
        self._numbers.setFixedWidth(panel.selected_width)
        self._directions = QCheckBox()
        self._directions.setToolTip(tr("Show line directions according to the stored geometry"))
        self._directions.setFixedWidth(panel.selected_width)
        self._directions.setEnabled(layer.geometryType() == Qgis.GeometryType.Line)
        self._snap = QToolButton()
        self._snap.setCheckable(True)
        self._snap.setAutoRaise(True)
        self._snap.setIcon(panel.magnet_icon)
        self._snap.setFixedWidth(panel.snap_width)
        self._snap_menu = QMenu(self)
        self._type_actions = []
        for snap_type, label in snapping.SNAPPING_TYPES:
            action = self._snap_menu.addAction(tr(label))
            action.setCheckable(True)
            action.triggered.connect(
                lambda on, snap_type=snap_type: self._panel.set_layer_type(self.layer_id, snap_type, on))
            self._type_actions.append((snap_type, action))
        self._snap_menu.aboutToShow.connect(self._refresh_type_actions)
        self._snap.setMenu(self._snap_menu)
        self._snap.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(ROW_MARGIN, 1, ROW_MARGIN, 1)
        layout.setSpacing(ROW_SPACING)
        layout.addWidget(self._visible)
        layout.addWidget(self._layer_icon)
        layout.addWidget(self._name, 1)
        layout.addWidget(self._warning)
        layout.addWidget(self._no_selection)
        layout.addWidget(self._color)
        layout.addWidget(self._shape)
        layout.addWidget(self._selected, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._numbers, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._directions, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._snap)

        self.set_name(layer.name())
        self.refresh()
        self.refresh_snapping()

        self._visible.toggled.connect(lambda on: self._state.set_enabled(self.layer_id, on))
        self._color.colorChanged.connect(lambda color: self._state.set_color(self.layer_id, color))
        self._shape.currentIndexChanged.connect(
            lambda index: self._state.set_shape(self.layer_id, self._shape.itemData(index)))
        self._selected.toggled.connect(lambda on: self._state.set_selected_only(self.layer_id, on))
        self._numbers.toggled.connect(lambda on: self._state.set_numbers(self.layer_id, on))
        self._directions.toggled.connect(lambda on: self._state.set_directions(self.layer_id, on))
        self._snap.clicked.connect(lambda on: self._panel.set_layer_snapping(self.layer_id, on))

    def set_name(self, name):
        self._name.set_full_text(name)

    def refresh(self):
        color = self._state.color(self.layer_id)
        shape = self._state.shape(self.layer_id)
        for widget in (self._visible, self._color, self._shape, self._selected, self._numbers, self._directions):
            widget.blockSignals(True)
        self._visible.setChecked(self._state.is_enabled(self.layer_id))
        self._selected.setChecked(self._state.is_selected_only(self.layer_id))
        self._numbers.setChecked(self._state.show_numbers(self.layer_id))
        self._directions.setChecked(self._state.show_directions(self.layer_id))
        self._color.setColor(color)
        for index in range(self._shape.count()):
            self._shape.setItemIcon(index, shape_icon(self._shape.itemData(index), color))
        self._shape.setCurrentIndex(max(self._shape.findData(shape), 0))
        for widget in (self._visible, self._color, self._shape, self._selected, self._numbers, self._directions):
            widget.blockSignals(False)
        self._warning.setVisible(self._state.is_over_limit(self.layer_id))
        self._no_selection.setVisible(self._state.is_no_selection(self.layer_id))

    def refresh_snapping(self):
        on, tooltip = self._panel.snapping_status(self.layer_id)
        self._snap.blockSignals(True)
        self._snap.setChecked(on)
        self._snap.blockSignals(False)
        self._snap.setToolTip(tooltip)

    def _refresh_type_actions(self):
        types = self._panel.layer_types(self.layer_id)
        for snap_type, action in self._type_actions:
            action.setChecked(bool(types is not None and types & snap_type))


class _RowDelegate(QStyledItemDelegate):
    """Gives rows the height of a LayerRow and leaves the drawing to it."""

    def __init__(self, height, parent=None):
        super().__init__(parent)
        self._height = height

    def sizeHint(self, option, index):
        return QSize(0, self._height)

    def paint(self, painter, option, index):
        pass


class _LayerProxy(QgsMapLayerProxyModel):
    """Native geometry/name filtering, with visibility and checked-first sorting."""

    def __init__(self, state, project, parent):
        self._state = state
        self._project = project
        super().__init__(parent)
        self.setProject(project)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._refresh)
        state.layerChanged.connect(self.schedule_refresh)
        state.configReset.connect(self.schedule_refresh)
        state.listFilterChanged.connect(self.schedule_refresh)
        project.layerTreeRoot().visibilityChanged.connect(self.schedule_refresh)
        project.layerTreeRoot().addedChildren.connect(self.schedule_refresh)
        project.layerTreeRoot().removedChildren.connect(self.schedule_refresh)
        self.sort(0)

    def schedule_refresh(self, *_):
        # Reorder after the checkbox signal finishes, keeping row widgets alive
        # throughout the current mouse/keyboard event.
        self._timer.start(0)

    def _refresh(self):
        self.invalidateFilter()
        self.invalidate()

    def filterAcceptsRow(self, row, parent):
        if not super().filterAcceptsRow(row, parent):
            return False
        if not self._state.visible_layers_only():
            return True
        layer = self.sourceModel().data(self.sourceModel().index(row, 0, parent),
                                        QgsMapLayerModel.ItemDataRole.Layer)
        node = self._project.layerTreeRoot().findLayer(layer.id()) if layer is not None else None
        return node is not None and node.isVisible()

    def lessThan(self, left, right):
        role = QgsMapLayerModel.ItemDataRole.Layer
        first = self.sourceModel().data(left, role)
        second = self.sourceModel().data(right, role)
        if first is not None and second is not None:
            checked_first = self._state.is_requested(first.id())
            checked_second = self._state.is_requested(second.id())
            if checked_first != checked_second:
                return checked_first
        return super().lessThan(left, right)


class LayerPanel(QWidget):
    def __init__(self, state, project, toggle_action, dialog_parent, dock_callback=None, parent=None):
        super().__init__(parent)
        self.state = state
        self.magnet_icon = magnet_icon()
        self.color_width = COLOR_BUTTON_WIDTH
        shape_combo = QComboBox()
        for shape in SHAPES:
            shape_combo.addItem(shape_icon(shape, QColor(Qt.GlobalColor.black)), tr(SHAPE_LABELS[shape]))
        self.shape_width = shape_combo.sizeHint().width()
        snap_button = QToolButton()
        snap_button.setIcon(self.magnet_icon)
        snap_button.setMenu(QMenu(snap_button))
        snap_button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.snap_width = snap_button.sizeHint().width()
        self.selected_width = max(QCheckBox().sizeHint().width(), 22)
        self._project = project
        self._dialog_parent = dialog_parent

        toggle_button = QToolButton()
        toggle_button.setDefaultAction(toggle_action)
        toggle_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        toggle_button.setAutoRaise(True)

        self._size = QSpinBox()
        self._size.setRange(MARKER_SIZE_MIN, MARKER_SIZE_MAX)
        self._size.setSuffix(" px")
        self._size.setToolTip(tr("Size of the vertex markers of all layers"))
        self._size.setValue(state.marker_size())

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(toggle_button)
        header.addStretch(1)
        header.addWidget(QLabel(tr("Vertex Size")))
        header.addWidget(self._size)
        if dock_callback is not None:
            dock_button = QToolButton()
            dock_button.setIcon(QgsApplication.getThemeIcon("/mDockify.svg"))
            dock_button.setToolTip(tr("Dock panel"))
            dock_button.setAutoRaise(True)
            dock_button.clicked.connect(dock_callback)
            header.addWidget(dock_button)

        self._filter = QgsFilterLineEdit()
        self._filter.setShowSearchIcon(True)
        self._filter.setPlaceholderText(tr("Filter layers…"))

        self._layer_filter = QComboBox()
        self._layer_filter.addItem(tr("All Layers"), False)
        self._layer_filter.addItem(tr("Visible Layers"), True)
        self._layer_filter.setToolTip(tr("Layer list: all layers or layers visible in the QGIS layer tree (including parent groups)"))
        self._layer_filter.setCurrentIndex(1 if state.visible_layers_only() else 0)
        filter_row = QHBoxLayout()
        filter_row.addWidget(self._filter, 1)
        filter_row.addWidget(self._layer_filter)

        self._proxy = _LayerProxy(state, project, self)
        self._proxy.setFilters(Qgis.LayerFilter.HasGeometry)
        self._view = QTreeView()
        self._view.setHeaderHidden(True)
        self._view.setRootIsDecorated(False)
        self._view.setUniformRowHeights(True)
        self._view.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._view.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._view.setItemDelegate(_RowDelegate(QComboBox().sizeHint().height() + 4, self._view))
        self._view.setModel(self._proxy)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addLayout(header)
        layout.addLayout(filter_row)
        layout.addLayout(self._column_header())
        layout.setSpacing(4)
        layout.addWidget(self._view, 1)
        layout.addWidget(self._footer())
        self.setMinimumSize(490, 320)

        self._filter.textChanged.connect(self._proxy.setFilterString)
        self._layer_filter.currentIndexChanged.connect(
            lambda index: state.set_visible_layers_only(bool(self._layer_filter.itemData(index))))
        state.listFilterChanged.connect(self._show_layer_filter)
        self._size.valueChanged.connect(state.set_marker_size)
        self._proxy.rowsInserted.connect(lambda _parent, first, last: self._create_rows(first, last))
        self._proxy.modelReset.connect(self._create_all_rows)
        self._proxy.layoutChanged.connect(self._create_all_rows)
        state.layerChanged.connect(self._refresh_layer)
        state.layerRenamed.connect(self._rename_row)
        state.statusChanged.connect(self._refresh_layer)
        state.snappingChanged.connect(self._refresh_all_snapping)
        state.markerSizeChanged.connect(self._show_marker_size)
        state.configReset.connect(self._refresh_all)
        self._create_all_rows()

    def _column_header(self):
        """Column titles aligned with the controls of the LayerRow widgets."""
        frame = self._view.frameWidth()
        header = QHBoxLayout()
        header.setContentsMargins(ROW_MARGIN + frame, 0, ROW_MARGIN + frame, 0)
        header.setSpacing(ROW_SPACING)
        header.addWidget(QLabel(tr("Layer")), 1)
        for title, width, tooltip in (
            (tr("Color"), self.color_width, tr("Vertex marker color")),
            (tr("Shape"), self.shape_width, tr("Vertex marker shape")),
        ):
            label = QLabel(title)
            label.setFixedWidth(width)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setToolTip(tooltip)
            header.addWidget(label)
        # Icon titles keep the narrow columns short in every language.
        selected_header = QLabel()
        selected_header.setPixmap(QgsApplication.getThemeIcon("/mActionSelectRectangle.svg").pixmap(16, 16))
        selected_header.setFixedWidth(self.selected_width)
        selected_header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        selected_header.setToolTip(tr("Selected features only — vertices, indices and directions"))
        self.selected_header = selected_header
        header.addWidget(selected_header)
        for filename, tooltip in (("vertex_numbers.svg", "Show Vertex Indices"),
                                  ("direction.svg", "Show Line Directions")):
            label = QLabel()
            label.setPixmap(QIcon(os.path.join(os.path.dirname(__file__), "icons", filename)).pixmap(16, 16))
            label.setFixedWidth(self.selected_width)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setToolTip(tr(tooltip))
            header.addWidget(label)
        self.snap_header = QLabel()
        self.snap_header.setPixmap(QgsApplication.getThemeIcon("/mIconSnapping.svg").pixmap(16, 16))
        self.snap_header.setFixedWidth(self.snap_width)
        self.snap_header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.snap_header.setToolTip(tr("Layer snapping in the project snapping configuration; "
                                       "the arrow selects the snapping types"))
        header.addWidget(self.snap_header)
        # Keeps the titles above their columns while the list shows a scroll bar.
        scroll_bar = self._view.verticalScrollBar()
        scroll_spacer = QWidget()
        scroll_spacer.setFixedWidth(scroll_bar.sizeHint().width())
        scroll_spacer.setVisible(False)
        scroll_bar.rangeChanged.connect(lambda minimum, maximum: scroll_spacer.setVisible(maximum > minimum))
        header.addWidget(scroll_spacer)
        return header

    @staticmethod
    def _footer():
        footer = QWidget()
        layout = QVBoxLayout(footer)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(separator)
        label = QLabel()
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setText('<span style="color:#666666; font-size:8pt;">'
                      "Visible Vertices &middot; &copy;2026 Micha&#322; Sikora</span>")
        layout.addWidget(label)
        return footer

    def focus_filter(self):
        QTimer.singleShot(0, self._filter.setFocus)

    # rows: one LayerRow index widget per proxy row; the view deletes the
    # widgets of rows that are filtered out, so they are recreated on insert.

    def _create_all_rows(self):
        self._create_rows(0, self._proxy.rowCount() - 1)

    def _create_rows(self, first, last):
        for row in range(first, last + 1):
            index = self._proxy.index(row, 0)
            if self._view.indexWidget(index) is not None:
                continue
            layer = self._proxy.data(index, QgsMapLayerModel.ItemDataRole.Layer)
            if layer is not None:
                self._view.setIndexWidget(index, LayerRow(self, layer))

    def _rows(self):
        for row in range(self._proxy.rowCount()):
            widget = self._view.indexWidget(self._proxy.index(row, 0))
            if widget is not None:
                yield widget

    def _row(self, layer_id):
        layer = self._project.mapLayer(layer_id)
        if layer is None:
            return None
        index = self._proxy.mapFromSource(self._proxy.sourceLayerModel().indexFromLayer(layer))
        return self._view.indexWidget(index) if index.isValid() else None

    def _rename_row(self, layer_id):
        row = self._row(layer_id)
        if row is not None:
            row.set_name(self._project.mapLayer(layer_id).name())

    def _refresh_layer(self, layer_id):
        row = self._row(layer_id)
        if row is not None:
            row.refresh()

    def _refresh_all(self):
        self._show_marker_size(self.state.marker_size())
        for row in self._rows():
            row.refresh()
            row.refresh_snapping()

    def _refresh_all_snapping(self):
        for row in self._rows():
            row.refresh_snapping()

    def _show_marker_size(self, size):
        self._size.blockSignals(True)
        self._size.setValue(size)
        self._size.blockSignals(False)

    def _show_layer_filter(self, visible_only):
        self._layer_filter.blockSignals(True)
        self._layer_filter.setCurrentIndex(1 if visible_only else 0)
        self._layer_filter.blockSignals(False)

    # snapping

    def snapping_status(self, layer_id):
        layer = self._project.mapLayer(layer_id)
        if layer is None:
            return False, ""
        if snapping.is_layer_snapping_on(self._project, layer):
            types = snapping.layer_types(self._project, layer)
            names = ", ".join(tr(label) for snap_type, label in snapping.SNAPPING_TYPES if types & snap_type)
            return True, tr("Snapping is on for this layer: {}").format(names)
        if snapping.needs_mode_switch(self._project):
            return False, tr("Enable snapping for this layer.\nThe project snapping mode is '{}'; "
                             "per-layer snapping needs '{}'.").format(self._mode_name(), tr("Advanced Configuration"))
        return False, tr("Enable snapping for this layer")

    def layer_types(self, layer_id):
        layer = self._project.mapLayer(layer_id)
        return snapping.layer_types(self._project, layer) if layer is not None else None

    def set_layer_snapping(self, layer_id, on):
        self._change_snapping(layer_id, on, lambda layer: snapping.set_layer_snapping(self._project, layer, on))

    def set_layer_type(self, layer_id, snap_type, on):
        self._change_snapping(
            layer_id, on, lambda layer: snapping.set_layer_type(self._project, layer, snap_type, on))

    def _change_snapping(self, layer_id, on, change):
        layer = self._project.mapLayer(layer_id)
        if layer is not None and (not on or not snapping.needs_mode_switch(self._project)
                                  or self._confirm_mode_switch()):
            change(layer)
        # The configuration may be unchanged (no signal), so always show the real state.
        row = self._row(layer_id)
        if row is not None:
            row.refresh_snapping()

    def _mode_name(self):
        mode = snapping.current_mode(self._project)
        return tr(snapping.MODE_NAMES[mode]) if mode in snapping.MODE_NAMES else str(mode)

    def _confirm_mode_switch(self):
        text = tr(
            "Per-layer snapping works only in the '{advanced}' snapping mode. "
            "The project currently uses '{current}'.\n\n"
            "Switch the project to '{advanced}'? Layers will then snap only according to "
            "their individual settings in the advanced snapping configuration. Those settings, the "
            "tolerances and the other snapping options are kept, and you can switch the mode back "
            "in the Snapping toolbar."
        ).format(advanced=tr("Advanced Configuration"), current=self._mode_name())
        answer = QMessageBox.question(
            self._dialog_parent, "Visible Vertices", text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        return answer == QMessageBox.StandardButton.Yes
