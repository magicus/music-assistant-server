# Lyrion Test Infrastructure

Detta bibliotek innehaller delad testinfrastruktur for Lyrion-relaterade providers,
inklusive `lyrion_music` nu och `lyrion_player` senare.

## Delar

- `catalog_seed.py`: Delad, deterministisk testkatalog (artister/albums/tracks/genres/playlists)
  som anvands av bade fake LMS och Docker-seed.
- `fake_lms_server.py`: In-memory fake LMS med JSON-RPC, stream-URL och artwork-endpoints.
- `fixtures.py`: Gemensamma pytest-fixtures that exponerar en LMS-endpoint.

## Endpoint-val

Tester valjer endpoint sa har:

1. Om `--live-lyrion-docker` ar satt anvands Docker LMS (lokalt).
2. Annars startas fake LMS automatiskt pa en ledig local port.

## Teststrategi

- `tests/providers/lyrion_music/test_provider_blackbox.py`
  ar endpoint-agnostiska tester som kan koras mot fake LMS (default)
  eller Docker LMS med `--live-lyrion-docker`.
- `tests/providers/lyrion_music/test_provider_contract.py`
  ar fake-LMS-specifika tester (whitebox) som validerar intern
  request-historik och felinjicering i fake-servern.

I CI kor dessa tester i fake-lage. Docker-lage ar en lokal, aktiv
verifiering for arbete med Lyrion-specifik funktionalitet.

## Korning

Fake-lage (default):

```bash
python -m pytest tests/providers/lyrion_music -q
```

Kora enbart endpoint-agnostiska blackbox-tester i fake-lage:

```bash
python -m pytest tests/providers/lyrion_music/test_provider_blackbox.py -q
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

Kor blackbox mot Docker:

```bash
pytest tests/providers/lyrion_music/test_provider_blackbox.py --live-lyrion-docker -q
```

Eller via markor:

```bash
pytest -m live_lyrion_docker --live-lyrion-docker -q
```

Detta kor blackbox-tester markerade med `live_lyrion_docker`.

Fake-LMS-kontraktstesterna hoppas over automatiskt i Docker-lage:

- `tests/providers/lyrion_music/test_provider_contract.py`

Notera:

- Avsett for lokal, manuell korning (inte CI-gate).
- LMS-image kan overridas med `LMS_IMAGE`, t.ex.
  `LMS_IMAGE=lmscommunity/lyrionmusicserver:stable pytest -m live_lyrion_docker --live-lyrion-docker -q`
- Standardendpointen ar `http://127.0.0.1:9000` men kan overridas med `LYRION_TEST_DOCKER_LMS_URL`.
- Satt `LYRION_TEST_DOCKER_KEEP_RUNNING=1` om du vill lamna containern uppe efter testkorn.

## Utbyggnad for lyrion_player

Fake-servern ar avsiktligt stateful och utbyggbar. Nasta steg kan vara:

- spelarlivscykel (connect/disconnect),
- playback state/transport-kommandon,
- event-stream/cometd-emulering.
