"""Map canvas overlay: one point-type QgsRubberBand per displayed reference layer.

A rubber band keeps its points in canvas CRS coordinates and paints them in
C++, so a few bands with tens of thousands of points stay cheap to redraw.
Only features intersecting the visible extent are read; refreshes are
debounced and run synchronously, so they never overlap.
"""

import traceback
from functools import partial

from qgis.core import (
    Qgis,
    QgsCoordinateTransform,
    QgsCsException,
    QgsFeatureRequest,
    QgsGeometry,
    QgsPointXY,
    QgsRectangle,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.gui import QgsRubberBand
from qgis.PyQt.QtCore import QObject, QTimer
from qgis.PyQt.QtGui import QColor

from .state import log, tr
from .decorations import GeometryDecorations, line_arrows, MAX_DECORATIONS, MAX_SCANNED_VERTICES

REFRESH_DELAY_MS = 150
# Above this many vertices in view a layer is not drawn (the panel shows a warning).
MAX_VERTICES_PER_LAYER = 50000
# Features with at least this many vertices that reach outside the view are clipped to it.
CLIP_MIN_VERTICES = 256

# Markers are drawn as outlines in the layer color with a transparent fill.
# The hollow ICON_DIAMOND is drawn as an open shape in QGIS 3.40 and 4.0, so the
# "full" icons are used with a transparent fill instead.
ICONS = {
    "circle": QgsRubberBand.IconType.ICON_CIRCLE,
    "square": QgsRubberBand.IconType.ICON_FULL_BOX,
    "diamond": QgsRubberBand.IconType.ICON_FULL_DIAMOND,
    "cross": QgsRubberBand.IconType.ICON_CROSS,
    "x": QgsRubberBand.IconType.ICON_X,
}
MARKER_STROKE_WIDTH = 2

LAYER_SIGNALS = (
    "geometryChanged",
    "featureAdded",
    "featureDeleted",
    "dataChanged",
    "subsetStringChanged",
    "crsChanged",
)


class VertexOverlay(QObject):
    def __init__(self, canvas, state, project):
        super().__init__()
        self._canvas = canvas
        self._state = state
        self._project = project
        self._bands = {}  # layer id -> QgsRubberBand
        self._watched = {}  # layer id -> (layer, [(signal name, slot)])
        self._selected_only = {}  # layer id -> "selected only" mode the band was filled with
        self._dirty = set()
        self._logged = set()  # (layer id, problem) already written to the log
        self._decorations = {}  # layer id -> batched arrows / labels

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(REFRESH_DELAY_MS)
        self._timer.timeout.connect(self._refresh)

        self._connections = (
            (canvas.extentsChanged, self._on_view_changed),
            (canvas.destinationCrsChanged, self._on_view_changed),
            (canvas.rotationChanged, self._on_view_changed),
            (canvas.scaleChanged, self._on_view_changed),
            (state.globalChanged, self._sync_all),
            (state.configReset, self._sync_all),
            (state.layerChanged, self._sync_layer),
            (state.layerRemoved, self._drop_layer),
            (state.markerSizeChanged, self._restyle_all),
        )
        for signal, slot in self._connections:
            signal.connect(slot)
        self._sync_all()

    def unload(self):
        self._timer.stop()
        for signal, slot in self._connections:
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        for layer_id in list(self._watched):
            self._drop_layer(layer_id)

    # which layers have a band

    def _sync_all(self, *_):
        wanted = set(self._state.displayed_layer_ids()) if self._state.is_global_on() else set()
        for layer_id in set(self._watched) - wanted:
            self._drop_layer(layer_id)
        for layer_id in wanted:
            self._sync_layer(layer_id)

    def _sync_layer(self, layer_id):
        if not (self._state.is_global_on() and self._state.is_requested(layer_id)):
            self._drop_layer(layer_id)
            return
        layer = self._project.mapLayer(layer_id)
        if not isinstance(layer, QgsVectorLayer) or not layer.isSpatial():
            return
        self._watch(layer)
        if self._state.is_enabled(layer_id):
            if layer_id not in self._bands:
                self._bands[layer_id] = QgsRubberBand(self._canvas, Qgis.GeometryType.Point)
            self._apply_style(self._bands[layer_id], layer_id)
        else:
            band = self._bands.pop(layer_id, None)
            if band is not None:
                self._canvas.scene().removeItem(band)
        decorated = (self._state.show_numbers(layer_id) or
                     (self._state.show_directions(layer_id) and layer.geometryType() == Qgis.GeometryType.Line))
        if decorated:
            if layer_id not in self._decorations:
                self._decorations[layer_id] = GeometryDecorations(self._canvas)
            self._decorations[layer_id].clear()
        else:
            item = self._decorations.pop(layer_id, None)
            if item is not None:
                self._canvas.scene().removeItem(item)
        self._schedule({layer_id})

    def _drop_layer(self, layer_id):
        band = self._bands.pop(layer_id, None)
        if band is not None:
            self._canvas.scene().removeItem(band)
        item = self._decorations.pop(layer_id, None)
        if item is not None:
            self._canvas.scene().removeItem(item)
        self._unwatch(layer_id)
        self._dirty.discard(layer_id)
        self._selected_only.pop(layer_id, None)
        self._state.set_over_limit(layer_id, False)
        self._state.set_no_selection(layer_id, False)

    def _watch(self, layer):
        if layer.id() in self._watched:
            return
        data_slot = partial(self._on_layer_data_changed, layer.id())
        connections = [(name, data_slot) for name in LAYER_SIGNALS]
        connections.append(("selectionChanged", partial(self._on_selection_changed, layer.id())))
        connections.append(("willBeDeleted", partial(self._on_layer_deleted, layer.id())))
        for name, slot in connections:
            getattr(layer, name).connect(slot)
        self._watched[layer.id()] = (layer, connections)

    def _on_layer_deleted(self, layer_id, *_):
        self._drop_layer(layer_id)

    def _unwatch(self, layer_id):
        layer, connections = self._watched.pop(layer_id, (None, ()))
        for name, slot in connections:
            try:
                getattr(layer, name).disconnect(slot)
            except (TypeError, RuntimeError):
                pass

    # styling (no feature access)

    def _apply_style(self, band, layer_id):
        band.setIcon(ICONS[self._state.shape(layer_id)])
        band.setIconSize(self._state.marker_size())
        band.setStrokeColor(self._state.color(layer_id))
        band.setFillColor(QColor(0, 0, 0, 0))
        band.setWidth(MARKER_STROKE_WIDTH)
        band.update()

    def _restyle_all(self, *_):
        for layer_id, band in self._bands.items():
            self._apply_style(band, layer_id)
        self._schedule(set(self._decorations))

    # refresh scheduling

    def _on_view_changed(self, *_):
        for item in self._decorations.values():
            item.clear()
        self._schedule(set(self._watched))

    def _on_layer_data_changed(self, layer_id, *_):
        if layer_id in self._decorations:
            self._decorations[layer_id].clear()
        self._schedule({layer_id})

    def _on_selection_changed(self, layer_id, *_):
        if self._state.is_selected_only(layer_id):
            self._on_layer_data_changed(layer_id)

    def _schedule(self, layer_ids):
        self._dirty |= layer_ids
        self._timer.start()  # restarting the single-shot timer debounces bursts

    def _refresh(self):
        layer_ids = self._dirty & set(self._watched)
        self._dirty = set()
        settings = self._canvas.mapSettings()
        view_extent = settings.visibleExtent()
        canvas_crs = settings.destinationCrs()
        for layer_id in layer_ids:
            band = self._bands.get(layer_id)
            layer = self._project.mapLayer(layer_id)
            selected_only = self._state.is_selected_only(layer_id)
            self._selected_only[layer_id] = selected_only
            no_selection = selected_only and isinstance(layer, QgsVectorLayer) and not layer.selectedFeatureCount()
            self._state.set_no_selection(layer_id, no_selection)
            try:
                over_limit = self._fill(band, layer, view_extent, canvas_crs, selected_only) if band else False
            except Exception:
                if band is not None:
                    band.reset(Qgis.GeometryType.Point)
                over_limit = False
                self._log_once(layer_id, "error", tr("Could not collect vertices of layer {}:\n{}").format(
                    layer_id, traceback.format_exc()), Qgis.MessageLevel.Critical)
            self._state.set_over_limit(layer_id, over_limit)
            if layer_id in self._decorations:
                try:
                    self._fill_decorations(settings, layer_id, selected_only)
                except Exception:
                    self._decorations[layer_id].clear()
                    self._log_once(layer_id, "decorations", traceback.format_exc(), Qgis.MessageLevel.Critical)

    def _fill_decorations(self, settings, layer_id, selected_only=False):
        item = self._decorations[layer_id]
        layer = self._project.mapLayer(layer_id)
        if (not isinstance(layer, QgsVectorLayer) or not layer.isValid()
                or (selected_only and not layer.selectedFeatureCount())):
            item.clear()
            return
        transform = QgsCoordinateTransform(layer.crs(), settings.destinationCrs(), self._project)
        search = self._search_rect(layer, transform, settings.visibleExtent())
        if search is None or search.isEmpty():
            item.clear()
            return
        request = QgsFeatureRequest().setFilterRect(search).setNoAttributes()
        if selected_only:
            request.setFilterFids(layer.selectedFeatureIds())
        arrows, labels = [], []
        scanned = 0
        limited = False
        width, height = self._canvas.width(), self._canvas.height()
        to_pixel = settings.mapToPixel()
        directions = self._state.show_directions(layer_id) and layer.geometryType() == Qgis.GeometryType.Line

        def pixel(vertex):
            point = to_pixel.transform(QgsPointXY(vertex))
            return point.x(), point.y()

        for feature in layer.getFeatures(request):
            geometry = feature.geometry()
            if geometry.isNull() or geometry.isEmpty():
                continue
            # Never clip or segmentize the geometry used for numbering: QGIS's
            # flat vertex index includes all preceding parts, rings and controls.
            scanned += geometry.constGet().nCoordinates()
            if scanned > MAX_SCANNED_VERTICES:
                limited = True
                break
            if not transform.isShortCircuited():
                try:
                    geometry.transform(transform)
                except QgsCsException:
                    self._log_once(layer.id(), "decoration-transform",
                                   tr("Some active-layer decorations could not be transformed."),
                                   Qgis.MessageLevel.Warning)
                    continue
            if self._state.show_numbers(layer_id):
                for index, vertex in enumerate(geometry.vertices()):
                    x, y = pixel(vertex)
                    if 0 <= x <= width and 0 <= y <= height:
                        labels.append((x, y, index))
                        if len(labels) >= MAX_DECORATIONS:
                            limited = True
                            break
            if directions:
                # Work on a separate representation for curved line tangents.
                line = QgsGeometry(geometry)
                line.convertToStraightSegment()
                for part in line.constParts():
                    points = [pixel(vertex) for vertex in part.vertices()]
                    arrows.extend(line_arrows(points, width, height, MAX_DECORATIONS - len(arrows)))
                    if len(arrows) >= MAX_DECORATIONS:
                        limited = True
                        break
            if limited:
                break
        item.set_data(arrows, labels, self._state.marker_size())
        if limited:
            self._log_once(layer.id(), "decoration-limit",
                           tr("Layer decorations limited to 10000 arrows/labels and 250000 "
                           "source vertices per view. Zoom in to inspect the remaining geometry."),
                           Qgis.MessageLevel.Warning)

    # vertex collection

    def _fill(self, band, layer, view_extent, canvas_crs, selected_only=False):
        """Fills the band with the vertices of layer features in view; returns True when over the limit.

        With selected_only only selected features are used, and nothing when none is selected.
        """
        band.reset(Qgis.GeometryType.Point)
        if not isinstance(layer, QgsVectorLayer) or not layer.isValid() or not layer.isSpatial():
            return False
        if selected_only and not layer.selectedFeatureCount():
            return False
        transform = QgsCoordinateTransform(layer.crs(), canvas_crs, self._project)
        search_rect = self._search_rect(layer, transform, view_extent)
        if search_rect is None or search_rect.isEmpty():
            return False

        request = QgsFeatureRequest().setFilterRect(search_rect).setNoAttributes()
        if selected_only:
            request.setFilterFids(layer.selectedFeatureIds())
        reproject = not transform.isShortCircuited()
        total = 0
        failed = 0
        for feature in layer.getFeatures(request):
            geometry = self._vertex_geometry(feature.geometry(), search_rect)
            if geometry is None:
                continue
            total += geometry.constGet().nCoordinates()
            if total > MAX_VERTICES_PER_LAYER:
                band.reset(Qgis.GeometryType.Point)
                return True
            if reproject:
                try:
                    geometry.transform(transform)
                except QgsCsException:
                    failed += 1
                    continue
            band.addGeometry(geometry, None, False)
        band.updatePosition()
        band.update()
        if failed:
            self._log_once(layer.id(), "transform", tr("Layer '{}': {} feature(s) could not be transformed to the "
                           "map CRS and were skipped.").format(layer.name(), failed), Qgis.MessageLevel.Warning)
        return False

    def _search_rect(self, layer, transform, view_extent):
        """The visible extent expressed in the layer CRS, or None when it cannot be computed."""
        if transform.isShortCircuited():
            return QgsRectangle(view_extent)
        transform = QgsCoordinateTransform(transform)
        transform.setBallparkTransformsAreAppropriate(True)
        try:
            return transform.transformBoundingBox(view_extent, Qgis.TransformDirection.Reverse)
        except QgsCsException:
            pass
        # The view reaches outside the area the layer CRS can represent:
        # search only the part of the view covered by the layer.
        try:
            covered = view_extent.intersect(transform.transformBoundingBox(layer.extent()))
            if covered.isEmpty():
                return None
            return transform.transformBoundingBox(covered, Qgis.TransformDirection.Reverse)
        except QgsCsException as e:
            self._log_once(layer.id(), "extent", tr("Layer '{}': the map extent could not be transformed to the "
                           "layer CRS ({}); its vertices are not shown.").format(layer.name(), e),
                           Qgis.MessageLevel.Warning)
            return None

    @staticmethod
    def _vertex_geometry(geometry, search_rect):
        """Geometry whose vertices are drawn, in layer CRS; None when nothing is left."""
        if geometry.isNull() or geometry.isEmpty():
            return None
        if QgsWkbTypes.isCurvedType(geometry.wkbType()):
            # The rubber band would draw the segmentized curve; keep the real vertices instead.
            geometry = QgsGeometry.fromMultiPointXY([QgsPointXY(vertex) for vertex in geometry.vertices()])
        if (geometry.constGet().nCoordinates() >= CLIP_MIN_VERTICES
                and not search_rect.contains(geometry.boundingBox())):
            # A large feature reaching outside the view: keep only its vertices inside the view.
            geometry = geometry.convertToType(Qgis.GeometryType.Point, True).clipped(search_rect)
            if geometry.isNull() or geometry.isEmpty():
                return None
        return geometry

    def _log_once(self, layer_id, problem, message, level):
        if (layer_id, problem) not in self._logged:
            self._logged.add((layer_id, problem))
            log(message, level)
