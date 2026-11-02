def classFactory(iface):
    from .plugin import VisibleVerticesPlugin

    return VisibleVerticesPlugin(iface)
