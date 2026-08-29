"""Doc2Agent package."""

from .ingest import IngestService
from .pipeline import Doc2AgentPipeline

__all__ = ["Doc2AgentPipeline", "IngestService"]
__version__ = "0.1.0"
