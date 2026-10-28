from dataclasses import dataclass

from model.working_time.schedule import Schedule


@dataclass(frozen=True)
class ScheduleSuggestion:
    group: str
    schedule: Schedule
    actions: int
    coverage: float           # az aktivitás ekkora része esik a javasolt munkaidőbe
    weekend_share: float
