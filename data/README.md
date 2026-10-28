# data/

A futás adatai. A mappa tartalma nem kerül a repóba (valódi céges adat, illetve abból vagy generálással készül).

| Fájl | Mi ez | Ki írja |
|---|---|---|
| `data.xlsx` | A ServiceNow incidens-history export (munkalap: `inc_history`). | kézzel ide másolva |
| `transformed_timeline.json` | Az Excelből transzformált idősor (gyorsítótár). | `simulation/time_transformer.py` |
| `synthetic_timeline*.json` | Szintetikus előtörténet beültetett hibákkal / hibák nélkül, és a megoldókulcs. | `python -m simulation.synthetic.build_synthetic` |
| `simulation_source.json` | A kiválasztott lejátszási forrás. | `simulation/timeline_selector.py` |
| `database.json` | Incidensek (history-val), issue-k, issue-előzmény. | `storage/incident_repository.py` |
| `thresholds.json` | A detektorok küszöbei, ablakai, be/ki, jóváhagyásos mód. | `storage/threshold_store.py` |
| `knowledge.json` | A tudástár: tények, almérések, szűkebb küszöbök, tanulságok. | `storage/knowledge_base.py` |
| `discovery_state.json` | A tanulási állapot: jelöltek, döntések, szabályok. | `learning/discovery_service.py` |
| `backups/` | A tudás és a tanulási állapot datált mentései. | `storage/backup.py` |
| `learning_demo.json` | A tanulási szimuláció eredménye. | `python -m simulation.experiments.learning_demo` |

Az útvonalak egy helyen vannak megadva: `config/paths.py`.
