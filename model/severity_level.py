from enum import Enum


class SeverityLevel(Enum):
    INFORMATIONAL = "INFORMATIONAL"
    EXCEEDED_MODERATE = "EXCEEDED_MODERATE"
    EXCEEDED_EXTREME = "EXCEEDED_EXTREME"

    def __str__(self):
        return self.value
