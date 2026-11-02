"""Per-layer snapping on top of the native project snapping configuration.

Visible Vertices does not snap by itself. It edits a copy of the project's
QgsSnappingConfig and changes only the individual settings of one layer
(plus, after the caller has confirmed it, the snapping mode), so tolerances,
units, scale limits, other layers and the global options are preserved.
Per-layer settings are only effective in the "Advanced Configuration" mode.
"""

from qgis.core import Qgis, QgsSnappingConfig

VERTEX = Qgis.SnappingType.Vertex
ADVANCED = Qgis.SnappingMode.AdvancedConfiguration

# Types offered in the magnet menu (all exist in QGIS 3.40 and 4.0).
SNAPPING_TYPES = (
    (Qgis.SnappingType.Vertex, "Vertex"),
    (Qgis.SnappingType.Segment, "Segment"),
    (Qgis.SnappingType.MiddleOfSegment, "Middle of Segments"),
    (Qgis.SnappingType.Centroid, "Centroid"),
    (Qgis.SnappingType.Area, "Area"),
)

MODE_NAMES = {
    Qgis.SnappingMode.ActiveLayer: "Active Layer",
    Qgis.SnappingMode.AllLayers: "All Layers",
    ADVANCED: "Advanced Configuration",
}


def is_layer_snapping_on(project, layer):
    """True when the layer effectively snaps through its own advanced settings."""
    config = project.snappingConfig()
    if not config.enabled() or config.mode() != ADVANCED:
        return False
    settings = config.individualLayerSettings(layer)
    return settings.valid() and settings.enabled() and bool(settings.typeFlag())


def layer_types(project, layer):
    """Snapping types stored for the layer (also while its entry is disabled); None without an entry."""
    settings = project.snappingConfig().individualLayerSettings(layer)
    return settings.typeFlag() if settings.valid() else None


def needs_mode_switch(project):
    return project.snappingConfig().mode() != ADVANCED


def current_mode(project):
    return project.snappingConfig().mode()


def set_layer_snapping(project, layer, on):
    """Turns the layer's advanced snapping entry on or off.

    On: the entry is enabled with its stored types, or with Vertex when it has
    none; the project is switched to Advanced Configuration and project
    snapping is enabled, because without both the entry has no effect (the
    caller must confirm a mode switch with the user). Off: only the entry is
    disabled, its types stay stored.
    """
    config = project.snappingConfig()
    settings = config.individualLayerSettings(layer)
    if on:
        if not settings.valid():
            settings = _new_settings(config, VERTEX)
        elif not settings.typeFlag():
            settings.setTypeFlag(VERTEX)
        settings.setEnabled(True)
        _activate(config)
    else:
        if not settings.valid() or not settings.enabled():
            return
        settings.setEnabled(False)
    config.setIndividualLayerSettings(layer, settings)
    project.setSnappingConfig(config)


def set_layer_type(project, layer, snap_type, on):
    """Adds or removes one snapping type of the layer, leaving its other types alone.

    Adding a type also enables the entry (see set_layer_snapping). Removing the
    last type disables the entry.
    """
    config = project.snappingConfig()
    settings = config.individualLayerSettings(layer)
    if on:
        if not settings.valid():
            settings = _new_settings(config, snap_type)
        else:
            settings.setTypeFlag(settings.typeFlag() | snap_type)
        settings.setEnabled(True)
        _activate(config)
    else:
        if not settings.valid():
            return
        remaining = settings.typeFlag() & ~snap_type
        settings.setTypeFlag(remaining)
        if not remaining:
            settings.setEnabled(False)
    config.setIndividualLayerSettings(layer, settings)
    project.setSnappingConfig(config)


def _new_settings(config, snap_type):
    return QgsSnappingConfig.IndividualLayerSettings(True, snap_type, config.tolerance(), config.units(), 0.0, 0.0)


def _activate(config):
    config.setMode(ADVANCED)
    config.setEnabled(True)
