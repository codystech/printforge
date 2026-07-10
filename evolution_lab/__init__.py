"""Isolated PrintForge evolution/training-lab backend.

The package is inert until an application explicitly calls :func:`create_router`.
It never mutates PrintForge's production library and it never performs model-weight
training.  Production integration supplies narrow callbacks through
``EvolutionAdapters``.
"""

from .config import EvolutionLabConfig
from .engine import EvolutionAdapters, EvolutionEngine
from .router import create_router
from .store import EvolutionStore

__all__ = [
    "EvolutionAdapters",
    "EvolutionEngine",
    "EvolutionLabConfig",
    "EvolutionStore",
    "create_router",
]
