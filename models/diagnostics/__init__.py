"""Incident diagnosztikák, mind a core keretrendszeren (models.diagnostics.core).

A konkrét diagnosztikákat a moduljukból importáljuk (pl. models.diagnostics.ping_pong); a csomag
szándékosan nem tölti be őket, mert egy részük a feltárásra épül, a feltárás pedig a diagnosztikák
segédmoduljaira (körkörös import). A szerkezet leírása: docs/DIAGNOSTICS.md.
"""
