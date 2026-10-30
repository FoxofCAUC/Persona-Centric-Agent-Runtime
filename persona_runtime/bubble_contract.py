"""Frozen v3 limits shared by the prompt, wire schema and local validator."""
from dataclasses import dataclass


@dataclass(frozen=True)
class BubbleContract:
    schema_version: int = 3
    presentation: str = 'model-bubbles-v1'
    max_messages: int = 12
    item_characters: int = 8000
    total_characters: int = 12000
    kinds: tuple = ('chat', 'technical', 'code', 'quote')
    intents: tuple = ('respond', 'clarify', 'support', 'explain', 'acknowledge')


V3 = BubbleContract()
