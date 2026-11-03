from model.diagnostics.core.base_diagnosis import BaseDiagnosis
from model.diagnostics.core.window_type import WindowType


class CrossTicketDiagnosis(BaseDiagnosis):
    """Több incidens közös mintázatát vizsgáló diagnosztika, a saját mérési ablakán belül.

    A család csak a besorolást és az alapablakot adja; a konkrét diagnosztika maga írja meg az `_observe`-ot,
    mert a jegyek kiválasztása (ki számít az ablakba, mi az összehasonlító populáció) detektoronként más:
    a legtöbbnek az ablakon kívüli előzmény is kell (pl. saját norma, utolsó használat).
    """

    family = "CROSS_TICKET"
    affected_type = "incident"
    window_type = WindowType.SLIDING
