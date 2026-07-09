# Video Konvertor

Jednoduchá webová appka (FastAPI + uv): uživatel nahraje jedno nebo více
videí (max 100 MB na soubor podle `config.toml`), zadá tajné heslo, zvolí
typ konverze/rozmazání/rozlišení a videa se pomocí ffmpeg postupně
zkonvertují na 16:9. UI zobrazuje živý průběh nahrávání i konverze a každé
hotové video se rovnou stáhne (a jde stáhnout i opakovaně, dokud job
nevyprší).

## Konfigurace

```bash
cp config.example.toml config.toml
# a nastav upload_password
```

`config.toml` je v `.gitignore` (obsahuje tajné heslo) — verzuje se jen
`config.example.toml`. Přehled voleb:

```toml
[security]
upload_password = "zmen-me-na-tajne-heslo"   # heslo nutné pro nahrání videa

[upload]
max_size_mb = 100
work_dir = ""                  # prázdné = systémový temp adresář
job_ttl_seconds = 3600          # jak dlouho žije job od poslední aktivity
sweep_interval_seconds = 600    # jak často se work_dir kontroluje

[ffmpeg]
binary = "ffmpeg"
probe_binary = "ffprobe"
threads = 2                     # na kolik CPU jader je omezené kódování
timeout_seconds = 1800
```

## Jak to funguje (API)

Frontend nekonvertuje v jednom dlouhém požadavku, ale přes joby, aby šlo
zobrazit skutečný průběh:

1. `POST /auth` (form: `password`) — ověření hesla; frontend volá před
   začátkem fronty, aby se u špatného hesla zbytečně nenahrával celý soubor.
2. `POST /jobs` (multipart: `password`, `mode`, `blur`, `resolution`,
   `file`) — uloží video a hned vrátí `{"job_id": "..."}` (202), konverze
   běží na pozadí. Konverze běží vždy jen jedna naráz, další čekají ve
   frontě.
3. `GET /jobs/{job_id}` — stav (`uploaded` / `converting` / `done` /
   `error`) a `progress` v procentech (spočítané z délky videa přes
   `ffprobe` a živého `ffmpeg -progress` výstupu).
4. `GET /jobs/{job_id}/download` — vrátí hotové video; jde volat opakovaně
   a každé stažení obnoví TTL jobu.

Frontend (`static/index.html`) umí vybrat víc souborů najednou (drag &
drop), zpracovává je postupně a u každého ukazuje průběh nahrávání i
konverze; hotová videa stahuje automaticky.

## Typy konverze

Oba převádí video na 16:9, liší se tím, čím se doplní prázdné místo:

- `to_16_9_blur` — rozmazané/ztemnělé pozadí z téhož videa (míra
  rozmazání volitelná: slabé/střední/silné)
- `to_16_9_black_bars` — klasický černý letterbox/pillarbox

Rozlišení (`resolution`): `auto` (výchozí — dopočítá se z delší hrany
zdroje, Full HD zdroj → Full HD výstup), nebo pevně `720p` / `1080p` / `4k`.

Výstup je vždy MP4: H.264 (`yuv420p`, takže ho přehraje i starší
hardware/prohlížeč), audio překódované do AAC (kopie by selhala u kodeků,
které MP4 neumí — Opus/Vorbis/PCM) a `+faststart` (video jde přehrávat
hned, bez stažení celého souboru).

Další režimy lze přidat v `app/converter.py` (enum `ConversionMode` +
`build_ffmpeg_args`) a doplnit do `<select>` v `static/index.html`.

## Spuštění lokálně

Vyžaduje [uv](https://docs.astral.sh/uv/) a nainstalovaný `ffmpeg`/`ffprobe`
v PATH.

```bash
uv sync
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Poté otevři `http://localhost:8000`.

## Testy

```bash
uv run pytest
```

End-to-end testy přes FastAPI TestClient s reálným ffmpeg (generují si
krátké testovací video). Heslo si berou z načtené konfigurace.

## Spuštění v Dockeru

Image obsahuje ffmpeg, `config.toml` se do image nekopíruje — mountuje se
při běhu:

```bash
docker build -t konvertor .

docker run -d --name konvertor -p 8000:8000 \
  -v "$(pwd)/config.toml:/app/config.toml:ro" \
  --tmpfs /tmp:rw,size=2g \
  konvertor
```

`--tmpfs /tmp` je nepovinné, ale doporučené — dočasné soubory (upload i
výstup) se pak drží v RAM, ne na disku. Image má healthcheck na `/config`.

## Pozor

Joby se drží jen v paměti procesu, takže server musí běžet jako **jediný
worker** (ne `--workers 2+`, ne víc replik za load balancerem bez sticky
session), jinak by dotaz na stav mohl padnout na jiný proces, než který
job vytvořil.

## Bezpečnostní poznámky

- Heslo se porovnává přes `hmac.compare_digest` na bytes (ochrana proti
  timing attacku, funguje i pro hesla s diakritikou).
- Neúspěšné pokusy o heslo jsou globálně brzděné na ~1 pokus/s
  (ochrana proti brute-force).
- Limit velikosti se vynucuje podle `Content-Length` (který je pro
  `POST /jobs` povinný — jinak 411, aby chunked upload neobešel limit)
  a znovu při zápisu na disk.
- Nahraný i zkonvertovaný soubor žijí v dočasném adresáři jobu
  (`work_dir/<job_id>/`); mažou se po `job_ttl_seconds` od poslední
  aktivity (dokončení/stažení). Rozběhnutý job se nikdy nemaže. Úklid
  běží periodicky i při startu serveru a je bezpečný i při více
  instancích nad stejným work_dir.
- Kódování je omezené na `threads` CPU jader, aby konverze nesebrala
  serveru celý výkon.
