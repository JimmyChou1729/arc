"""Durable page-mapped OCR proofreading for ARC."""

from .project import ProofreadProject, ProofreadProjectError
from .service import ProofreadService, ProofreadServiceError
from .source import MineruSource, ProofreadSourceError, load_mineru_source
from .workflow import ProofreadHandler

__version__ = "1.0.4"

__all__ = [
    "MineruSource",
    "ProofreadHandler",
    "ProofreadProject",
    "ProofreadProjectError",
    "ProofreadService",
    "ProofreadServiceError",
    "ProofreadSourceError",
    "load_mineru_source",
]
