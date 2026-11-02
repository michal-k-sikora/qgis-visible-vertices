"""Visible Vertices plugin lifecycle: toolbar, popup, dock, layer context menu and wiring."""

import os

from qgis.core import Qgis, QgsApplication, QgsProject, QgsVectorLayer
from qgis.gui import QgsDockWidget
from qgis.PyQt.QtCore import QCoreApplication, Qt, QTimer, QTranslator
from qgis.PyQt.QtGui import QAction, QIcon
from qgis.PyQt.QtWidgets import QMenu, QToolButton, QWidgetAction

from .overlay import VertexOverlay
from .panel import LayerPanel
from .state import VerticesState, tr

PLUGIN_NAME = "Visible Vertices"
MENU_NAME = "&Visible Vertices"


class VisibleVerticesPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.translator = self._load_translator()
        self.state = None
        self.overlay = None
        self.toggle_action = None
        self.panel_action = None
        self.layer_action = None
        self.menu = None
        self.popup_panel = None
        self.button = None
        self.toolbar = None
        self.dock = None

    @staticmethod
    def _load_translator():
        locale = (QgsApplication.locale() or "")[:2]
        path = os.path.join(os.path.dirname(__file__), "i18n", "visible_vertices_{}.qm".format(locale))
        if not os.path.exists(path):
            return None
        translator = QTranslator()
        if not translator.load(path):
            return None
        QCoreApplication.installTranslator(translator)
        return translator

    def initGui(self):
        main_window = self.iface.mainWindow()
        project = QgsProject.instance()
        self.state = VerticesState(project)
        self.overlay = VertexOverlay(self.iface.mapCanvas(), self.state, project)

        icon = QIcon(os.path.join(os.path.dirname(__file__), "icons", "icon.svg"))
        self.toggle_action = QAction(icon, PLUGIN_NAME, main_window)
        self.toggle_action.setCheckable(True)
        self.toggle_action.setToolTip(tr("Show or hide the vertices of the layers checked in the Visible Vertices "
                                         "panel (open it with the arrow next to this button)"))
        self.toggle_action.toggled.connect(self.state.set_global_on)
        self.toggle_action.triggered.connect(self._on_toggle_triggered)
        self.state.globalChanged.connect(self.toggle_action.setChecked)

        self.panel_action = QAction(QgsApplication.getThemeIcon("/mDockify.svg"), tr("Visible Vertices Panel"),
                                    main_window)
        self.panel_action.triggered.connect(self.show_dock)

        self.menu = QMenu(main_window)
        self.popup_panel = LayerPanel(self.state, project, self.toggle_action, main_window,
                                      dock_callback=self.show_dock)
        panel_action = QWidgetAction(self.menu)
        panel_action.setDefaultWidget(self.popup_panel)
        self.menu.addAction(panel_action)
        self.menu.aboutToShow.connect(self.popup_panel.focus_filter)

        self.button = QToolButton()
        self.button.setDefaultAction(self.toggle_action)
        self.button.setMenu(self.menu)
        self.button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.toolbar = self.iface.addToolBar(PLUGIN_NAME)
        self.toolbar.setObjectName("VisibleVerticesToolbar")
        self.toolbar.addWidget(self.button)

        self.iface.addPluginToMenu(MENU_NAME, self.toggle_action)
        self.iface.addPluginToMenu(MENU_NAME, self.panel_action)

        # "Show Vertices" in the context menu of vector layers in the Layers panel.
        self.layer_action = QAction(icon, tr("Show Vertices"), main_window)
        self.layer_action.setCheckable(True)
        self.layer_action.triggered.connect(self._on_layer_action_triggered)
        self.iface.addCustomActionForLayerType(self.layer_action, "", Qgis.LayerType.Vector, True)
        self.iface.layerTreeView().contextMenuAboutToShow.connect(self._update_layer_action)

        self.state.dockStateRestored.connect(self._restore_dock)
        if self.state.is_dock_open():
            self.show_dock()

    # layer context menu

    def _selected_spatial_layers(self):
        return [layer for layer in self.iface.layerTreeView().selectedLayers()
                if isinstance(layer, QgsVectorLayer) and layer.isSpatial()]

    def _update_layer_action(self, *_):
        layers = self._selected_spatial_layers()
        self.layer_action.setEnabled(bool(layers))
        self.layer_action.setChecked(bool(layers) and all(self.state.is_enabled(layer.id()) for layer in layers))

    def _on_layer_action_triggered(self, checked):
        for layer in self._selected_spatial_layers():
            self.state.set_enabled(layer.id(), checked)

    # toolbar button, popup and dock

    def _on_toggle_triggered(self, checked):
        """Switching on with no layer checked shows nothing, so open the configuration instead."""
        if not checked or self.state.displayed_layer_ids() or self.menu.isVisible():
            return
        if self.dock is not None and self.dock.isUserVisible():
            return
        if self.button.isVisible():
            QTimer.singleShot(0, self.button.showMenu)
        else:
            self.show_dock()

    def show_dock(self):
        self.menu.hide()
        if self.dock is None:
            self.dock = QgsDockWidget(PLUGIN_NAME, self.iface.mainWindow())
            self.dock.setObjectName("VisibleVerticesDock")
            self.dock.setWidget(LayerPanel(self.state, QgsProject.instance(), self.toggle_action,
                                           self.iface.mainWindow()))
            self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock)
            self.dock.closedStateChanged.connect(self._on_dock_closed_state_changed)
        self.dock.setUserVisible(True)

    def _on_dock_closed_state_changed(self, closed):
        self.state.set_dock_open(not closed)

    def _restore_dock(self, dock_open):
        """A project was opened: show the dock as it was when that project was used last."""
        if dock_open:
            self.show_dock()
        elif self.dock is not None:
            self.dock.setUserVisible(False)

    def unload(self):
        if self.translator is not None:
            QCoreApplication.removeTranslator(self.translator)
            self.translator = None
        if self.state is None:
            return
        self.menu.hide()
        try:
            self.iface.layerTreeView().contextMenuAboutToShow.disconnect(self._update_layer_action)
        except (TypeError, RuntimeError):
            pass
        self.iface.removeCustomActionForLayerType(self.layer_action)
        self.iface.removePluginMenu(MENU_NAME, self.toggle_action)
        self.iface.removePluginMenu(MENU_NAME, self.panel_action)
        self.iface.mainWindow().removeToolBar(self.toolbar)
        if self.dock is not None:
            # Removing the dock must not be stored as "closed by the user".
            self.dock.closedStateChanged.disconnect(self._on_dock_closed_state_changed)
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
        self.overlay.unload()
        self.state.unload()
        for obj in (self.toolbar, self.button, self.menu, self.toggle_action, self.panel_action, self.layer_action):
            obj.deleteLater()
        self.state = None
        self.overlay = None
        self.toggle_action = None
        self.panel_action = None
        self.layer_action = None
        self.menu = None
        self.popup_panel = None
        self.button = None
        self.toolbar = None
        self.dock = None
