"""Krita Coop: real-time cooperative drawing between Krita instances."""

import os

try:
    from krita import DockWidgetFactory, DockWidgetFactoryBase, Krita
except ImportError:  # imported outside Krita (unit tests)
    Krita = None

if Krita is not None and not os.environ.get("KRITA_COOP_NO_REGISTER"):
    from .docker import CoopDocker

    Krita.instance().addDockWidgetFactory(
        DockWidgetFactory("krita_coop_docker", DockWidgetFactoryBase.DockPosition.DockRight, CoopDocker))
