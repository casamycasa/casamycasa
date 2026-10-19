# SpaceBase3000 – presentationsenkät i Docker

En container som både visar formuläret och sparar svaren. Det behövs ingen databasserver eller annan tjänst vid sidan om.

## Kör

```bash
docker build -t spacebase-enkat .
docker run -d --name spacebase-enkat -p 8000:8000 spacebase-enkat
```

Öppna sedan **http://localhost:8000**. Kollegor på samma nätverk når den på `http://<datorns-ip>:8000`.

## Var svaren sparas

Svaren hamnar i en SQLite-fil i containern: `/data/spacebase.db`.

| Du gör | Svaren |
|---|---|
| `docker restart` / `docker stop` + `docker start` | finns kvar |
| `docker rm` eller bygger om och startar en ny container | försvinner |

Vill du att svaren ska överleva en ny container, starta den med en volym:

```bash
docker run -d --name spacebase-enkat -p 8000:8000 -v spacebase-data:/data spacebase-enkat
```

Kopiera ut databasen som backup:

```bash
docker cp spacebase-enkat:/data/spacebase.db ./spacebase-backup.db
```

## Inställningar

Sätts med `-e NAMN=värde` vid `docker run`.

| Variabel | Standard | Beskrivning |
|---|---|---|
| `OPEN_SESSIONS` | `true` | Alla som använder formuläret kan starta en ny omgång och ändra titel, presentatör och datum. Med `false` krävs `ADMIN_TOKEN`. |
| `ADMIN_TOKEN` | *(tom)* | Krävs för CSV-export, för att stänga eller radera omgångar och för att ändra frågorna. |
| `PORT` | `8000` | Port i containern. |
| `MAX_TEXT_LENGTH` | `2000` | Max antal tecken i fritextsvar. |
| `CORS_ORIGINS` | *(tom)* | Behövs bara om en annan sajt ska anropa API:t direkt. |

## Admin

Starta med en token: `-e ADMIN_TOKEN=$(openssl rand -hex 32)`.

```bash
# Lista omgångar med antal svar
curl http://localhost:8000/api/surveys/presentation-review/sessions

# Exportera svaren i en omgång som CSV (öppnas i Excel)
curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://localhost:8000/api/sessions/<id>/export.csv -o svar.csv

# Stäng en omgång för nya svar
curl -X PATCH -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"is_open": false}' http://localhost:8000/api/sessions/<id>
```

All API-dokumentation finns på http://localhost:8000/docs.

## Bra att veta

- **Typsnitt:** Space Mono hämtas från Google Fonts. Utan internet visas ett vanligt monospace-typsnitt i stället. Allt annat fungerar.
- **Anonymitet:** Varje webbläsare får ett slumpat id, så att en person kan ändra sitt svar utan att det räknas två gånger. Namn sparas bara om personen fyller i fråga 08. IP-adresser sparas inte.
- **Uppdatering:** Sammanställningen uppdateras var femte sekund.
