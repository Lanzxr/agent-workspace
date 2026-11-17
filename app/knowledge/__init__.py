"""Scoped Company Agent access to the shared AnythingLLM knowledge base."""

from .config import KnowledgeSettings
from .anythingllm import AnythingLLMClient
from .service import KnowledgeService

__all__ = ["AnythingLLMClient", "KnowledgeService", "KnowledgeSettings"]
