from enum import Enum


class RuleKind(str, Enum):
    REJECTION = "rejection"  # elvetett minta
    KNOWLEDGE = "knowledge"  # tényre épülő szakmai tudás
