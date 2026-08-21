"""Compatibility facade for :mod:`ac_document.workflows._llm`."""

from ac_document.workflows._llm import *  # noqa: F401,F403
from ac_document.workflows._llm import DocumentWorkflowError

PaperWorkflowError = DocumentWorkflowError
