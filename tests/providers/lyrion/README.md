# Lyrion Test Infrastructure

Detta bibliotek innehaller delad testinfrastruktur for Lyrion-relaterade providers,
inklusive `lyrion_music` nu och `lyrion_player` senare.

## Delar

- `fake_lms_server.py`: In-memory fake LMS med JSON-RPC, stream-URL och artwork-endpoints.
- `fixtures.py`: Gemensamma pytest-fixtures that exponerar en LMS-endpoint.

## Endpoint-val

Tester valjer endpoint sa har:

1. Om `LYRION_TEST_LMS_URL` ar satt (t.ex. `http://127.0.0.1:9000`) anvands den.
2. Annars om `LYRION_TEST_LMS_HOST` ar satt anvands den + `LYRION_TEST_LMS_PORT` (default `9000`).
3. Annars startas fake LMS automatiskt pa en ledig local port.

## Teststrategi

- `tests/providers/lyrion_music/test_provider_blackbox.py`
  ar endpoint-agnostiska tester som kan koras mot fake LMS (default)
  eller Docker LMS med `--live-lyrion-docker`.
- `tests/providers/lyrion_music/test_provider_contract.py`
  ar fake-LMS-specifika tester (whitebox) som validerar intern
  request-historik och felinjicering i fake-servern.
- `tests/providers/lyrion_music/test_provider_live_smoke.py`
  kor bara nar real LMS ar konfigurerad och gor data-agnostiska smoke-checks.

## Korning

Fake-lage (default):

```bash
python -m pytest tests/providers/lyrion_music -q
```

Kora enbart endpoint-agnostiska blackbox-tester i fake-lage:

```bash
python -m pytest tests/providers/lyrion_music/test_provider_blackbox.py -q
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

## Lokal Docker for real LMS (on demand)

For lokal verifiering nar du andrar Lyrion-kod finns en on-demand setup
dar pytest-fixturen skoter orkestreringen i Python:

- genererar ett litet deterministiskt musikbibliotek med ffmpeg
- startar LMS i Docker
- triggar rescan
- vantar tills katalogen ar synlig
- kor live integrationstesterna

Tillfalliga testartefakter skrivs under:

- `build/test/lyrion/`

Kor direkt med pytest:

```bash
pytest tests/providers/lyrion_music/test_provider_live_docker.py --live-lyrion-docker -q
```

Kora samma blackbox-tester mot Docker LMS:

```bash
pytest tests/providers/lyrion_music/test_provider_blackbox.py --live-lyrion-docker -q
```

Eller via markor:

```bash
pytest -m live_lyrion_docker --live-lyrion-docker -q
```

Detta kor:

- `tests/providers/lyrion_music/test_provider_live_docker.py`

Fake-LMS-kontraktstesterna hoppas over automatiskt i Docker-lage:

- `tests/providers/lyrion_music/test_provider_contract.py`

Notera:

- Avsett for lokal, manuell korning (inte CI-gate).
- LMS-image kan overridas med `LMS_IMAGE`, t.ex.
  `LMS_IMAGE=lmscommunity/lyrionmusicserver:stable pytest -m live_lyrion_docker --live-lyrion-docker -q`
- Standardendpointen ar `http://127.0.0.1:9000` men kan overridas med `LYRION_TEST_LMS_URL`.
- Satt `LYRION_TEST_DOCKER_KEEP_RUNNING=1` om du vill lamna containern uppe efter testkorn.

## Utbyggnad for lyrion_player

Fake-servern ar avsiktligt stateful och utbyggbar. Nasta steg kan vara:

- spelarlivscykel (connect/disconnect),
- playback state/transport-kommandon,
- event-stream/cometd-emulering.
