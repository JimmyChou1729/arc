"""Compatibility facade for :mod:`arc_document.workflows._llm`."""

from arc_document.workflows._llm import *  # noqa: F401,F403
from arc_document.workflows._llm import DocumentWorkflowError

PaperWorkflowError = DocumentWorkflowError
