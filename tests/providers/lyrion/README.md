# Lyrion Test Infrastructure

Detta bibliotek innehaller delad testinfrastruktur for Lyrion-relaterade providers,
inklusive `lyrion_music` nu och `lyrion_player` senare.

## Delar

- `fake_lms_server.py`: In-memory fake LMS med JSON-RPC, stream-URL och artwork-endpoints.
- `fixtures.py`: Gemensamma pytest-fixtures som exponerar en LMS-endpoint.

## Endpoint-val

Tester valjer endpoint sa har:

1. Om `LYRION_TEST_LMS_URL` ar satt (t.ex. `http://127.0.0.1:9000`) anvands den.
2. Annars om `LYRION_TEST_LMS_HOST` ar satt anvands den + `LYRION_TEST_LMS_PORT` (default `9000`).
3. Annars startas fake LMS automatiskt pa en ledig lokal port.

## Teststrategi

- `tests/providers/lyrion_music/test_provider_contract.py`
  kor mot fake LMS och antar deterministisk fejk-katalog.
- `tests/providers/lyrion_music/test_provider_live_smoke.py`
  kor bara nar real LMS ar konfigurerad och gor data-agnostiska smoke-checks.

## Korning

Fake-lage (default):

```bash
python -m pytest tests/providers/lyrion_music -q
```

Real LMS via URL:

```bash
LYRION_TEST_LMS_URL=http://192.168.1.10:9000 \
python -m pytest tests/providers/lyrion_music -q
```

Real LMS via host/port:

```bash
LYRION_TEST_LMS_HOST=192.168.1.10 LYRION_TEST_LMS_PORT=9000 \
python -m pytest tests/providers/lyrion_music -q
```

## Utbyggnad for lyrion_player

Fake-servern ar avsiktligt stateful och utbyggbar. Nasta steg kan vara:

- spelarlivscykel (connect/disconnect),
- playback state/transport-kommandon,
- event-stream/cometd-emulering.
