"""Shared deterministic Lyrion catalog seed data for tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ArtistSeed:
    """One seeded artist."""

    id: str
    name: str


@dataclass(frozen=True, slots=True)
class AlbumSeed:
    """One seeded album."""

    id: str
    name: str
    artist_id: str
    genre_id: str


@dataclass(frozen=True, slots=True)
class TrackSeed:
    """One seeded track."""

    id: str
    title: str
    artist_id: str
    album_id: str
    duration: int
    disc: int
    tracknum: int
    url: str | None = None


@dataclass(frozen=True, slots=True)
class GenreSeed:
    """One seeded genre."""

    id: str
    name: str


@dataclass(frozen=True, slots=True)
class PlaylistSeed:
    """One seeded playlist and its ordered track membership."""

    id: str
    name: str
    track_ids: tuple[str, ...]


ARTISTS: tuple[ArtistSeed, ...] = (
    ArtistSeed("a1", "DJ Home Azziztant"),
    ArtistSeed("a2", "The Async Awaiters"),
    ArtistSeed("a3", "One Hit Wonderbread"),
    ArtistSeed("a4", "Null Pointer Sisters"),
)

ALBUMS: tuple[AlbumSeed, ...] = (
    AlbumSeed("alb1", "Home Sweet Home Lab", "a1", "g1"),
    AlbumSeed("alb2", "Cache Me Outside", "a1", "g2"),
    AlbumSeed("alb3", "Awaiting Sunrise", "a2", "g1"),
    AlbumSeed("alb4", "Race Condition Blues", "a2", "g3"),
    AlbumSeed("alb5", "Greatest Hit And That's It", "a3", "g2"),
    AlbumSeed("alb6", "None Shall Pass", "a4", "g3"),
)

TRACKS: tuple[TrackSeed, ...] = (
    TrackSeed(
        "t1",
        "Wake Up And Smell The Exceptions",
        "a1",
        "alb1",
        211,
        1,
        1,
        "http://cdn.example.invalid/t1.mp3",
    ),
    TrackSeed("t2", "Kiss My Cache", "a1", "alb1", 199, 1, 2),
    TrackSeed("t3", "Cold Start Romance", "a1", "alb2", 187, 1, 1),
    TrackSeed("t4", "Await Me Maybe", "a2", "alb3", 241, 1, 1),
    TrackSeed("t5", "Future Is Pending", "a2", "alb3", 230, 1, 2),
    TrackSeed("t6", "Race You To The Lock", "a2", "alb4", 202, 1, 1),
    TrackSeed("t7", "Segfault Serenade", "a2", "alb4", 219, 2, 3),
    TrackSeed("t8", "Breadline Top 1", "a3", "alb5", 177, 1, 1),
    TrackSeed("t9", "None Shall Dance", "a4", "alb6", 222, 1, 1),
    TrackSeed("t10", "Guard Clause Cha-Cha", "a4", "alb6", 208, 1, 2),
)

GENRES: tuple[GenreSeed, ...] = (
    GenreSeed("g1", "Electro"),
    GenreSeed("g2", "Lo-Fi"),
    GenreSeed("g3", "Blues"),
)

PLAYLISTS: tuple[PlaylistSeed, ...] = (
    PlaylistSeed("pl1", "Debugging Bangers", ("t1", "t3", "t5", "t8")),
    PlaylistSeed("pl2", "Guard Clauses Only", ("t2", "t4", "t6", "t9", "t10")),
)


def fake_artists() -> list[dict[str, Any]]:
    """Return fake-LMS shaped artist rows."""
    return [{"id": artist.id, "artist": artist.name, "portraitid": artist.id} for artist in ARTISTS]


def fake_albums() -> list[dict[str, Any]]:
    """Return fake-LMS shaped album rows."""
    artist_name = {artist.id: artist.name for artist in ARTISTS}
    return [
        {
            "id": album.id,
            "album": album.name,
            "artist": artist_name[album.artist_id],
            "artist_id": album.artist_id,
            "genre_id": album.genre_id,
            "coverid": album.id,
        }
        for album in ALBUMS
    ]


def fake_tracks() -> list[dict[str, Any]]:
    """Return fake-LMS shaped track rows."""
    artist_name = {artist.id: artist.name for artist in ARTISTS}
    album_name = {album.id: album.name for album in ALBUMS}
    rows: list[dict[str, Any]] = []
    for track in TRACKS:
        row: dict[str, Any] = {
            "id": track.id,
            "title": track.title,
            "artist": artist_name[track.artist_id],
            "artist_id": track.artist_id,
            "album": album_name[track.album_id],
            "album_id": track.album_id,
            "duration": track.duration,
            "disc": track.disc,
            "tracknum": track.tracknum,
        }
        if track.url:
            row["url"] = track.url
        rows.append(row)
    return rows


def fake_playlists() -> list[dict[str, Any]]:
    """Return fake-LMS shaped playlist rows."""
    return [{"id": playlist.id, "playlist": playlist.name} for playlist in PLAYLISTS]


def fake_genres() -> list[dict[str, Any]]:
    """Return fake-LMS shaped genre rows."""
    return [{"id": genre.id, "genre": genre.name} for genre in GENRES]


def fake_playlist_tracks() -> dict[str, list[str]]:
    """Return fake-LMS playlist-to-track-id mapping."""
    return {playlist.id: list(playlist.track_ids) for playlist in PLAYLISTS}


def docker_catalog() -> tuple[dict[str, Any], ...]:
    """Return docker seed catalog grouped by album."""
    artist_name = {artist.id: artist.name for artist in ARTISTS}
    genre_name = {genre.id: genre.name for genre in GENRES}
    tracks_by_album: dict[str, list[tuple[str, int, int]]] = {album.id: [] for album in ALBUMS}
    for track in TRACKS:
        tracks_by_album[track.album_id].append((track.title, track.disc, track.tracknum))

    return tuple(
        {
            "artist": artist_name[album.artist_id],
            "album": album.name,
            "genre": genre_name[album.genre_id],
            "tracks": tuple(tracks_by_album[album.id]),
        }
        for album in ALBUMS
    )


def docker_playlists_by_name() -> dict[str, list[str]]:
    """Return docker playlist members keyed by playlist name."""
    track_title = {track.id: track.title for track in TRACKS}
    return {
        playlist.name: [track_title[track_id] for track_id in playlist.track_ids]
        for playlist in PLAYLISTS
    }
