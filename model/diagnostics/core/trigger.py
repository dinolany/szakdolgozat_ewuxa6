class Trigger:
    """A mérést indító események."""

    EVENT_BATCH = "EVENT_BATCH"              # feldolgozott eseményköteg után
    WINDOW_CLOSED = "WINDOW_CLOSED"          # lezárult egy TUMBLING vödör
    MANUAL = "MANUAL"                        # kézi indítás
    FULL_REANALYSIS = "FULL_REANALYSIS"      # teljes újraelemzés
    THRESHOLD_CHANGED = "THRESHOLD_CHANGED"  # küszöbparaméter változott
    WINDOW_CHANGED = "WINDOW_CHANGED"        # ablakparaméter változott
