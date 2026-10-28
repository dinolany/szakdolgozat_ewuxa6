from enum import Enum


class CandidateState(str, Enum):
    QUEUED = "queued"                # döntésre vár
    FILTERED = "filtered"            # egy szabály elrejtette
    ACCEPTED = "accepted"            # finding: issue lesz belőle
    REJECTED_ONCE = "rejected_once"  # elvetve erre az előfordulásra; új előfordulásnál visszajön
    REJECTED = "rejected"            # elvetve (szabály rögzíti)
    BASELINED = "baselined"          # ismert eltérés: csak a saját szokásos eltéréséhez képesti romlás jelez
    ATTACHED = "attached"            # egy nyitott issue (feladat) mintájához illik: az issue-hoz csatolva, nem kerül a sorba
    REFINED = "refined"              # tovább bontva: maga nem jelez, csak a benne (hozzá képest) kiugró szűkítései
