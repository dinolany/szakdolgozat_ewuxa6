from enum import Enum


class RuleAction(str, Enum):
    DROP = "drop"
    BOOST = "boost"
