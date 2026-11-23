# Kino Time

Berliner Kinoprogramm, gefiltert nach Startzeit, Fassung (OV / OmU / OmeU; ohne Auswahl alle inkl. Synchronfassungen) und Umkreis.

- `index.html` – die Website
- `data.json` – das aktuelle Programm (wird automatisch erzeugt)
- `scripts/scrape.py` – holt das Programm von indiekino.de und berlin.de, Filmdaten von TMDB und Wikidata
- `cache/` – zwischengespeicherte Kino- und Filmdaten, damit nicht jedes Mal alles neu abgerufen wird
- `.github/workflows/update.yml` – führt das Script zweimal täglich aus

Der TMDB-Schlüssel liegt als Repository-Secret `TMDB_TOKEN`, nicht im Code.

Vorstellungen ohne Fassungsangabe: sicher deutschsprachig → OV, Deutsch kommt vor oder keine Sprachinfo → OV?, sonst Synchronfassung.
