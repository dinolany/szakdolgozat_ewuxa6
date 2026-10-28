from enum import Enum


class IssueState(Enum):
    ACTIVE = "active"
    RESOLVED = "resolved"
    SUPPRESSED = "suppressed"
    DELETED = "deleted"
    NOT_RELEVANT = "not_relevant"  # mérve van, de a küszöb alatt marad
    OUTDATED = "outdated"          # időablakos mérés: az ablak elmúlt, már nincs mit mérni
    DECLINED = "declined"
    ARCHIVED = "archived"

    def __str__(self):
        return self.value
