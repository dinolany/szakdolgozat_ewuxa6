from enum import Enum


class Verdict(str, Enum):
    FINDING = "finding"
    NOT_FINDING = "not_finding"
    KNOWN_DEVIATION = "known_deviation"
    REFINE = "refine"                # a minta önmagában nem finding, de ezen belül keressen tovább
