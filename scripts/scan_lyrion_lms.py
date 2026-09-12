#!/usr/bin/env python3
"""Scan local network ranges for reachable Lyrion/LMS instances."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import socket
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import aiohttp
import ifaddr
from music_assistant_models.errors import SetupFailedError

from music_assistant.providers.lyrion.setup_flow import validate_lms_endpoint

DEFAULT_PORTS: tuple[int, ...] = (9000, 9090)
DEFAULT_TIMEOUT = 5.0
DEFAULT_CONCURRENCY = 64
DEFAULT_LIMIT_PER_NETWORK = 256


@dataclass(slots=True)
class ProbeResult:
    """
    Result of a successful LMS probe.

    :param host: IP address or hostname that was probed.
    :param port: Port that validated as an LMS endpoint.
    :param network: Network range that produced the candidate.
    """

    host: str
    port: int
    network: str


def _build_parser() -> argparse.ArgumentParser:
    """Return the command line parser for the LMS discovery probe."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--network",
        action="append",
        default=[],
        help="CIDR network to scan, may be provided multiple times",
    )
    parser.add_argument(
        "--host",
        action="append",
        default=[],
        help="Specific host to probe, may be provided multiple times",
    )
    parser.add_argument(
        "--port",
        action="append",
        type=int,
        default=[],
        help="Port to probe, may be provided multiple times (default: 9000, 9090)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help="Per-endpoint validation timeout in seconds",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help="Maximum concurrent endpoint probes",
    )
    parser.add_argument(
        "--limit-per-network",
        type=int,
        default=DEFAULT_LIMIT_PER_NETWORK,
        help="Maximum number of hosts to probe per detected local network",
    )
    return parser


def _normalize_ports(raw_ports: Sequence[int]) -> tuple[int, ...]:
    """Return validated probe ports in a stable order."""
    ports = raw_ports or DEFAULT_PORTS
    unique_ports: list[int] = []
    for port in ports:
        if port < 1 or port > 65535:
            msg = f"Invalid port: {port}"
            raise ValueError(msg)
        if port not in unique_ports:
            unique_ports.append(port)
    return tuple(unique_ports)


def _iter_local_networks(limit_per_network: int) -> Iterable[ipaddress.IPv4Network]:
    """
    Yield likely local IPv4 networks based on active adapters.

    :param limit_per_network: Maximum hosts allowed per yielded network.
    """
    seen: set[ipaddress.IPv4Network] = set()
    for adapter in ifaddr.get_adapters():
        for adapter_ip in adapter.ips:
            if adapter_ip.is_IPv6:
                continue
            if not isinstance(adapter_ip.ip, str):
                continue
            address = adapter_ip.ip
            if address.startswith(("127.", "169.254.")):
                continue
            network_prefix = adapter_ip.network_prefix
            if network_prefix is None:
                continue
            network = ipaddress.ip_network(f"{address}/{network_prefix}", strict=False)
            if not isinstance(network, ipaddress.IPv4Network):
                continue
            if network.num_addresses - 2 > limit_per_network:
                continue
            if network in seen:
                continue
            seen.add(network)
            yield network


def _parse_networks(raw_networks: Sequence[str]) -> tuple[ipaddress.IPv4Network, ...]:
    """Parse explicit IPv4 network arguments."""
    parsed: list[ipaddress.IPv4Network] = []
    for raw_network in raw_networks:
        network = ipaddress.ip_network(raw_network, strict=False)
        if not isinstance(network, ipaddress.IPv4Network):
            msg = f"Only IPv4 networks are supported for active scanning: {raw_network}"
            raise ValueError(msg)
        parsed.append(network)
    return tuple(parsed)


def _candidate_hosts(
    explicit_hosts: Sequence[str],
    explicit_networks: Sequence[ipaddress.IPv4Network],
    limit_per_network: int,
) -> tuple[tuple[str, str], ...]:
    """Return host candidates with the network label they came from."""
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()

    for host in explicit_hosts:
        stripped = host.strip()
        if not stripped or stripped in seen:
            continue
        seen.add(stripped)
        candidates.append((stripped, "explicit-host"))

    networks = explicit_networks or tuple(_iter_local_networks(limit_per_network))
    for network in networks:
        host_count = 0
        for host in network.hosts():
            host_str = str(host)
            if host_str in seen:
                continue
            seen.add(host_str)
            candidates.append((host_str, str(network)))
            host_count += 1
            if host_count >= limit_per_network:
                break
    return tuple(candidates)


async def _resolve_host(host: str) -> str | None:
    """Return the canonical IP address for a host when it resolves."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host,
            None,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror:
        return None

    for family, _, _, _, sockaddr in infos:
        if family == socket.AF_INET:
            return str(sockaddr[0])
    if infos:
        return str(infos[0][4][0])
    return None


async def _probe_candidate(
    http_session: aiohttp.ClientSession,
    host: str,
    port: int,
    network: str,
    timeout: float,
    limiter: asyncio.Semaphore,
) -> ProbeResult | None:
    """Validate one host:port as a real LMS endpoint."""
    async with limiter:
        try:
            await asyncio.wait_for(
                validate_lms_endpoint(
                    host=host,
                    port=port,
                    http_session=http_session,
                ),
                timeout=timeout,
            )
        except SetupFailedError, TimeoutError:
            return None
        return ProbeResult(host=host, port=port, network=network)


async def _run(args: argparse.Namespace) -> int:
    """Run the live LMS scan and print any validated endpoints."""
    try:
        ports = _normalize_ports(args.port)
        explicit_networks = _parse_networks(args.network)
    except ValueError as err:
        print(err)
        return 2

    candidates = _candidate_hosts(args.host, explicit_networks, args.limit_per_network)
    if not candidates:
        print("No scan candidates found. Pass --network or --host explicitly.")
        return 2

    timeout = aiohttp.ClientTimeout(total=args.timeout)
    limiter = asyncio.Semaphore(max(1, args.concurrency))
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as http_session:
        tasks = [
            _probe_candidate(http_session, host, port, network, args.timeout, limiter)
            for host, network in candidates
            for port in ports
        ]
        results = [result for result in await asyncio.gather(*tasks) if result is not None]

    if not results:
        print("No Lyrion/LMS endpoints detected.")
        print("Try a narrower network with --network 192.168.x.0/24 or a known host with --host.")
        return 1

    print("Validated Lyrion/LMS endpoints:")
    resolved_cache: dict[str, str | None] = {}
    for result in sorted(results, key=lambda item: (item.network, item.host, item.port)):
        if result.host not in resolved_cache:
            resolved_cache[result.host] = await _resolve_host(result.host)
        resolved = resolved_cache[result.host]
        if resolved and resolved != result.host:
            print(f"- {result.host}:{result.port} (resolved {resolved}, via {result.network})")
            continue
        print(f"- {result.host}:{result.port} (via {result.network})")
    return 0


def main() -> int:
    """Run the command line entrypoint."""
    parser = _build_parser()
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
