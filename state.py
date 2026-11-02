"""Shared runtime state of Visible Vertices and its project persistence.

The popup, the dock and the overlay all read and change this one object;
none of the widgets keeps its own copy of the configuration.
"""

import json

from qgis.core import Qgis, QgsMessageLog
from qgis.PyQt.QtCore import QCoreApplication, QObject, pyqtSignal
from qgis.PyQt.QtGui import QColor

LOG_TAG = "Visible Vertices"
PROJECT_SCOPE = "VisibleVertices"

SHAPES = ("circle", "square", "diamond", "cross", "x")
DEFAULT_SHAPE = "circle"

MARKER_SIZE_MIN = 3
MARKER_SIZE_MAX = 25
MARKER_SIZE_DEFAULT = 8

# Shown on rows of layers that have not been given a color yet.
UNASSIGNED_COLOR = QColor(160, 160, 160)
PALETTE = (
    "#e41a1c", "#377eb8", "#4daf4a", "#984ea3", "#ff7f00",
    "#00bcd4", "#a65628", "#f781bf", "#c0ca33", "#3949ab",
)


def log(message, level=Qgis.MessageLevel.Info):
    QgsMessageLog.logMessage(message, LOG_TAG, level)


def tr(text):
    """Translated user-facing text (one translation context for the whole plugin)."""
    return QCoreApplication.translate("VisibleVertices", text)


class LayerConfig:
    __slots__ = ("enabled", "color", "shape", "selected_only", "numbers", "directions")

    def __init__(self, enabled=False, color=None, shape=DEFAULT_SHAPE, selected_only=False,
                 numbers=False, directions=False):
        self.enabled = enabled
        self.color = color
        self.shape = shape
        self.selected_only = selected_only
        self.numbers = numbers
        self.directions = directions


class VerticesState(QObject):
    globalChanged = pyqtSignal(bool)
    layerChanged = pyqtSignal(str)  # visibility, color, shape or "selected only" of one layer
    layerRemoved = pyqtSignal(str)  # emitted while the layer still exists
    layerRenamed = pyqtSignal(str)  # QgsMapLayerModel itself does not signal renames
    markerSizeChanged = pyqtSignal(int)
    configReset = pyqtSignal()  # project read or cleared
    snappingChanged = pyqtSignal()  # native project snapping configuration changed
    statusChanged = pyqtSignal(str)  # runtime "too many vertices" / "nothing selected" flags of one layer
    dockStateRestored = pyqtSignal(bool)  # project read: whether its dock was open
    listFilterChanged = pyqtSignal(bool)

    def __init__(self, project):
        super().__init__()
        self._project = project
        self._layers = {}
        self._marker_size = MARKER_SIZE_DEFAULT
        self._global_on = False
        self._over_limit = set()
        self._no_selection = set()
        self._dock_open = False
        self._visible_layers_only = False

        project.readProject.connect(self._on_project_read)
        project.cleared.connect(self._on_project_cleared)
        project.layersAdded.connect(self._watch_names)
        project.layersWillBeRemoved.connect(self._on_layers_will_be_removed)
        project.snappingConfigChanged.connect(self._on_snapping_config_changed)
        self._watch_names(project.mapLayers().values())
        self.load()

    def unload(self):
        for signal, slot in (
            (self._project.readProject, self._on_project_read),
            (self._project.cleared, self._on_project_cleared),
            (self._project.layersAdded, self._watch_names),
            (self._project.layersWillBeRemoved, self._on_layers_will_be_removed),
            (self._project.snappingConfigChanged, self._on_snapping_config_changed),
        ):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        for layer in self._project.mapLayers().values():
            self._unwatch_name(layer)
        self._layers.clear()
        self._over_limit.clear()
        self._no_selection.clear()

    # global switch and marker size

    def is_global_on(self):
        return self._global_on

    def set_global_on(self, on):
        on = bool(on)
        if on != self._global_on:
            self._global_on = on
            self.globalChanged.emit(on)

    def marker_size(self):
        return self._marker_size

    def set_marker_size(self, size):
        size = min(max(int(size), MARKER_SIZE_MIN), MARKER_SIZE_MAX)
        if size != self._marker_size:
            self._marker_size = size
            self.save()
            self.markerSizeChanged.emit(size)

    # per-layer configuration

    def is_enabled(self, layer_id):
        config = self._layers.get(layer_id)
        return bool(config and config.enabled)

    def enabled_layer_ids(self):
        return [layer_id for layer_id, config in self._layers.items() if config.enabled]

    def displayed_layer_ids(self):
        return [layer_id for layer_id in self._layers if self.is_requested(layer_id)]

    def is_requested(self, layer_id):
        return self.is_enabled(layer_id) or self.show_numbers(layer_id) or self.show_directions(layer_id)

    def show_numbers(self, layer_id):
        config = self._layers.get(layer_id)
        return bool(config and config.numbers)

    def show_directions(self, layer_id):
        config = self._layers.get(layer_id)
        return bool(config and config.directions)

    def set_numbers(self, layer_id, on):
        self._set_decoration(layer_id, "numbers", on)

    def set_directions(self, layer_id, on):
        self._set_decoration(layer_id, "directions", on)

    def _set_decoration(self, layer_id, name, on):
        config = self._layers.setdefault(layer_id, LayerConfig())
        on = bool(on)
        if getattr(config, name) != on:
            setattr(config, name, on)
            self._layer_changed(layer_id)
            if on:
                self.set_global_on(True)

    def visible_layers_only(self):
        return self._visible_layers_only

    def set_visible_layers_only(self, on):
        on = bool(on)
        if on == self._visible_layers_only:
            return
        self._visible_layers_only = on
        was_dirty = self._project.isDirty()
        self._project.writeEntry(PROJECT_SCOPE, "visible_layers_only", on)
        if not was_dirty:
            self._project.setDirty(False)
        self.listFilterChanged.emit(on)

    def set_enabled(self, layer_id, enabled):
        config = self._layers.setdefault(layer_id, LayerConfig())
        enabled = bool(enabled)
        if config.enabled == enabled:
            return
        config.enabled = enabled
        if enabled and config.color is None:
            config.color = self._next_color()
        self._layer_changed(layer_id)
        if enabled:
            # Ticking a layer means "show me its vertices": switch the overlay on as well.
            self.set_global_on(True)

    def color(self, layer_id):
        config = self._layers.get(layer_id)
        if config is None or config.color is None:
            return QColor(UNASSIGNED_COLOR)
        return QColor(config.color)

    def set_color(self, layer_id, color):
        color = QColor(color)
        if not color.isValid():
            return
        config = self._layers.setdefault(layer_id, LayerConfig())
        if config.color is not None and config.color == color:
            return
        config.color = color
        self._layer_changed(layer_id)

    def shape(self, layer_id):
        config = self._layers.get(layer_id)
        return config.shape if config else DEFAULT_SHAPE

    def set_shape(self, layer_id, shape):
        if shape not in SHAPES:
            return
        config = self._layers.setdefault(layer_id, LayerConfig())
        if config.shape != shape:
            config.shape = shape
            self._layer_changed(layer_id)

    def is_selected_only(self, layer_id):
        config = self._layers.get(layer_id)
        return bool(config and config.selected_only)

    def set_selected_only(self, layer_id, selected_only):
        config = self._layers.setdefault(layer_id, LayerConfig())
        selected_only = bool(selected_only)
        if config.selected_only != selected_only:
            config.selected_only = selected_only
            self._layer_changed(layer_id)

    def is_dock_open(self):
        return self._dock_open

    def set_dock_open(self, dock_open):
        """Remembers in the project whether the dock is open, without marking the project modified."""
        dock_open = bool(dock_open)
        if dock_open == self._dock_open:
            return
        self._dock_open = dock_open
        was_dirty = self._project.isDirty()
        self._project.writeEntry(PROJECT_SCOPE, "dock_open", dock_open)
        if not was_dirty:
            self._project.setDirty(False)

    def is_over_limit(self, layer_id):
        return layer_id in self._over_limit

    def set_over_limit(self, layer_id, over):
        if over == (layer_id in self._over_limit):
            return
        if over:
            self._over_limit.add(layer_id)
        else:
            self._over_limit.discard(layer_id)
        self.statusChanged.emit(layer_id)

    def is_no_selection(self, layer_id):
        return layer_id in self._no_selection

    def set_no_selection(self, layer_id, no_selection):
        """Runtime flag: "selected only" is on but the layer has no selected features."""
        if no_selection == (layer_id in self._no_selection):
            return
        if no_selection:
            self._no_selection.add(layer_id)
        else:
            self._no_selection.discard(layer_id)
        self.statusChanged.emit(layer_id)

    def _next_color(self):
        used = {
            config.color.name()
            for config in self._layers.values()
            if config.enabled and config.color is not None
        }
        for name in PALETTE:
            if name not in used:
                return QColor(name)
        return QColor(PALETTE[len(used) % len(PALETTE)])

    def _layer_changed(self, layer_id):
        self.save()
        self.layerChanged.emit(layer_id)

    # persistence in the project file

    def load(self):
        """Read the configuration of the current project; stale layer ids are skipped."""
        self._layers = {}
        self._over_limit.clear()
        self._no_selection.clear()
        size, _ok = self._project.readNumEntry(PROJECT_SCOPE, "marker_size", MARKER_SIZE_DEFAULT)
        self._marker_size = min(max(size, MARKER_SIZE_MIN), MARKER_SIZE_MAX)
        self._dock_open, _ok = self._project.readBoolEntry(PROJECT_SCOPE, "dock_open", False)
        self._visible_layers_only, _ok = self._project.readBoolEntry(PROJECT_SCOPE, "visible_layers_only", False)

        raw, _ok = self._project.readEntry(PROJECT_SCOPE, "layers", "")
        if raw:
            try:
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError("not a JSON object")
            except ValueError as e:
                log(tr("Ignoring malformed layer configuration stored in the project: {}").format(e),
                    Qgis.MessageLevel.Warning)
                data = {}
            for layer_id, item in data.items():
                if not isinstance(item, dict) or self._project.mapLayer(layer_id) is None:
                    continue
                color = QColor(item["color"]) if isinstance(item.get("color"), str) else QColor()
                shape = item.get("shape")
                self._layers[layer_id] = LayerConfig(
                    enabled=item.get("enabled") is True,
                    color=color if color.isValid() else None,
                    shape=shape if shape in SHAPES else DEFAULT_SHAPE,
                    selected_only=item.get("selected_only") is True,
                    numbers=item.get("numbers") is True,
                    directions=item.get("directions") is True,
                )
        self.configReset.emit()
        self.listFilterChanged.emit(self._visible_layers_only)

    def save(self):
        data = {
            layer_id: {
                "enabled": config.enabled,
                "color": config.color.name(QColor.NameFormat.HexArgb) if config.color is not None else None,
                "shape": config.shape,
                "selected_only": config.selected_only,
                "numbers": config.numbers,
                "directions": config.directions,
            }
            for layer_id, config in self._layers.items()
        }
        self._project.writeEntry(PROJECT_SCOPE, "layers", json.dumps(data))
        self._project.writeEntry(PROJECT_SCOPE, "marker_size", self._marker_size)

    # project signals

    def _on_project_read(self, *_):
        self.load()
        self.dockStateRestored.emit(self._dock_open)

    def _on_project_cleared(self):
        # Every project starts with the overlay switched off.
        self.set_global_on(False)
        self._layers = {}
        self._over_limit.clear()
        self._no_selection.clear()
        self._marker_size = MARKER_SIZE_DEFAULT
        self._dock_open = False
        self._visible_layers_only = False
        self.configReset.emit()
        self.listFilterChanged.emit(False)

    def _watch_names(self, layers):
        for layer in layers:
            layer.nameChanged.connect(self._on_layer_renamed)

    def _unwatch_name(self, layer):
        try:
            layer.nameChanged.disconnect(self._on_layer_renamed)
        except (TypeError, RuntimeError):
            pass

    def _on_layer_renamed(self):
        layer = self.sender()
        if layer is not None:
            self.layerRenamed.emit(layer.id())

    def _on_layers_will_be_removed(self, layer_ids):
        # Runtime configuration only; the next save drops the entry from the project.
        for layer_id in layer_ids:
            layer = self._project.mapLayer(layer_id)
            if layer is not None:
                self._unwatch_name(layer)
            if self._layers.pop(layer_id, None) is not None:
                self._over_limit.discard(layer_id)
                self._no_selection.discard(layer_id)
                self.layerRemoved.emit(layer_id)

    def _on_snapping_config_changed(self, *_):
        self.snappingChanged.emit()
