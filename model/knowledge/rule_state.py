from enum import Enum


class RuleState(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"
