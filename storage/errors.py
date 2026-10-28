class RepositoryError(RuntimeError):
    """Az adatbázis-réteg hibáinak közös őse."""

class DatabaseCorruptedError(RepositoryError):
    """A database.json nem értelmezhető. Írás nem történik, amíg reset (clear) vagy kézi javítás nem jön."""

class DatabaseIOError(RepositoryError):
    """A database.json nem olvasható vagy nem írható (pl. egy másik program zárolja)."""
