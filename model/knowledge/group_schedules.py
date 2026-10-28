class GroupSchedules(dict):
    """Csoport -> munkaidő. A "*" kulcs a cégszintű munkaidő: annál a csoportnál számít, amelyiknek nincs saját
    (tanult vagy kézi) munkaideje. Az `in` csak a csoport saját munkaidejét nézi (a javaslatok ehhez mérnek)."""

    def get(self, key, default=None):
        """A csoport munkaideje: a saját, különben a cégszintű („*”), különben az alapérték.

        Hívja: BacklogAgingDiagnosis._age, DiscoveryService.backlog_calendar, children._schedule_of,
        profile.stint_profiles."""
        value = dict.get(self, key)
        if value is None and key != "*":
            value = dict.get(self, "*")
        return default if value is None else value
