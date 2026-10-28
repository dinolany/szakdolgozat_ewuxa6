from enum import Enum


class Scope(str, Enum):
    THIS_OCCURRENCE = "this_occurrence"
    THIS_PATTERN = "this_pattern"
    RULE = "rule"
