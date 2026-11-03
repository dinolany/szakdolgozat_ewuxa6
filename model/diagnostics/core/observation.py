from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple


@dataclass(frozen=True)
class Observation:
    """Amit a konkrét diagnosztika egy subjectről megállapít, küszöbtől függetlenül.

    Szerződés:
    - Ha egy subject nem mérhető (nincs adat, nem tartozik az ablakba), a diagnosztika nem ad róla megfigyelést;
      a megfigyelés tehát mindig érvényes mérés, és a besorolás (classify) csak a túllépésről dönt.
    - A `details` a diagnosztika saját számai, szabad kulcsokkal, néhány foglalt kulccsal:
      `entity` (a mért eset entitásai: csoport, útvonal, service… a szűkebb küszöbhöz és az elvető szabályhoz),
      `groups` (az issue csoportlistája). A base tölti: `threshold_source`, `thresholds_used`; az issue tölti:
      `measured_exceeded` és a tanuló kör kulcsai (model.issue.KEPT_DETAIL_KEYS). Ezeket a diagnosztika nem használja.
    """

    subject: str
    details: Mapping[str, Any]
    affected: Tuple[str, ...] = ()
    history_refs: Tuple[Dict[str, Any], ...] = ()
    trigger_ref: Optional[Dict[str, Any]] = None  # csak incidensalapú mérésnél: a küszöbátlépés eseménye
