"""End-to-end capstone test for the live config reconciler.

CONFIG_RECONCILER_SCOPE.md Phase 5: "mutate a file on disk ... wait up to
one poll interval, assert the running server's behavior changed -- no
restart, no Kubernetes." This drives the full stack through cli._run() --
real bound proxy port, real (mock) upstream proxies, real HTTP requests --
rather than TargetManager directly like the rest of this package's
integration tests, since the thing under test is specifically the
CLI-wired file-watch loop, not routing/retry logic those tests already
cover.
"""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import aiohttp
import pytest

from proxy_hopper.cli import _run
from proxy_hopper.config import ServerConfig


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait_for_port(host: str, port: int, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    last_exc: Exception | None = None
    while asyncio.get_running_loop().time() < deadline:
        try:
            reader, writer = await asyncio.open_connection(host, port)
            writer.close()
            await writer.wait_closed()
            return
        except OSError as exc:
            last_exc = exc
            await asyncio.sleep(0.02)
    raise TimeoutError(f"{host}:{port} never became reachable") from last_exc


def _write_config(root: Path, ip_list: list[str], target_names: list[str]) -> None:
    # Built as a flat list of already-correctly-indented lines rather than
    # a dedent()'d triple-quoted string -- dedent computes common leading
    # whitespace across *every* line including multi-line interpolated
    # blocks, which silently strips the wrong amount once those blocks
    # carry their own (different) indentation. See config_source.py's test
    # module for the same lesson learned during Phase 3.
    lines = ["proxyProviders:", "  - name: prov", "    ipList:"]
    lines += [f'      - "{ip}"' for ip in ip_list]
    lines += [
        "ipPools:",
        "  - name: pool",
        "    ipRequests:",
        "      - provider: prov",
        f"        count: {len(ip_list)}",
        "targets:",
    ]
    for name in target_names:
        lines += [
            f"  - name: {name}",
            f"    regex: '.*/{name}.*'",
            "    ipPool: pool",
            "    minRequestInterval: 0s",
        ]
    (root / "config.yaml").write_text("\n".join(lines) + "\n")


async def _request(
    proxy_host: str, proxy_port: int, upstream_url: str, path: str,
    force_ip: str | None = None,
) -> int:
    headers = {"X-Proxy-Hopper-Target": upstream_url}
    if force_ip:
        headers["X-Proxy-Hopper-Force-IP"] = force_ip
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"http://{proxy_host}:{proxy_port}{path}", headers=headers,
        ) as resp:
            return resp.status


class TestLiveFileEditNoRestart:
    @pytest.mark.asyncio
    async def test_new_target_added_on_disk_is_routable_without_restart(
        self, tmp_path, proxies, upstream
    ):
        _write_config(tmp_path, proxies.ip_list, target_names=["before"])

        proxy_port = _free_port()
        server = ServerConfig(
            host="127.0.0.1", port=proxy_port, backend="memory",
            metrics=False, probe=False,
            config_watch={"enabled": True, "interval_seconds": 0.1},
        )

        task = asyncio.create_task(_run([], [], server, config_path=tmp_path))
        try:
            await _wait_for_port("127.0.0.1", proxy_port)

            # The pre-existing target routes fine.
            status = await _request("127.0.0.1", proxy_port, upstream.url, "/before")
            assert status == 200

            # A URL only a not-yet-defined target would match currently has
            # nothing to route it -- 502, "No target configured".
            status = await _request("127.0.0.1", proxy_port, upstream.url, "/after")
            assert status == 502

            # Mutate the file on disk -- add the "after" target. No restart,
            # no new process, nothing touched but the file.
            _write_config(tmp_path, proxies.ip_list, target_names=["before", "after"])

            # Poll interval is 0.1s; give it a generous window.
            deadline = asyncio.get_running_loop().time() + 5.0
            last_status = None
            while asyncio.get_running_loop().time() < deadline:
                last_status = await _request("127.0.0.1", proxy_port, upstream.url, "/after")
                if last_status == 200:
                    break
                await asyncio.sleep(0.1)
            assert last_status == 200, (
                "new target from the edited file never became routable "
                "within the poll window"
            )

            # The original target is still unaffected.
            status = await _request("127.0.0.1", proxy_port, upstream.url, "/before")
            assert status == 200
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_provider_ip_removed_on_disk_stops_being_selectable(
        self, tmp_path, proxies, upstream
    ):
        """A provider IP removed from the file must stop being selectable.

        Checked with X-Proxy-Hopper-Force-IP against the *specific* removed
        address rather than sampling ordinary round-robin traffic -- retry
        logic could otherwise mask a stale pool entry statistically. Forcing
        a specific IP deterministically proves that exact address was
        retired from the live pool, which is what's actually new here (the
        file edit reaching the pool via reconcile), as opposed to
        already-tested general retry/routing behavior.
        """
        _write_config(tmp_path, proxies.ip_list, target_names=["t"])
        removed_ip = proxies[0].address
        surviving_ips = [proxies[1].address, proxies[2].address]

        proxy_port = _free_port()
        server = ServerConfig(
            host="127.0.0.1", port=proxy_port, backend="memory",
            metrics=False, probe=False,
            config_watch={"enabled": True, "interval_seconds": 0.1},
        )

        task = asyncio.create_task(_run([], [], server, config_path=tmp_path))
        try:
            await _wait_for_port("127.0.0.1", proxy_port)

            # Currently registered -- usable via force-IP.
            status = await _request(
                "127.0.0.1", proxy_port, upstream.url, "/t", force_ip=removed_ip
            )
            assert status == 200

            # Edit the file to drop that IP from the provider entirely.
            _write_config(tmp_path, surviving_ips, target_names=["t"])

            deadline = asyncio.get_running_loop().time() + 5.0
            last_status = 200
            while asyncio.get_running_loop().time() < deadline:
                last_status = await _request(
                    "127.0.0.1", proxy_port, upstream.url, "/t", force_ip=removed_ip
                )
                if last_status != 200:
                    break
                await asyncio.sleep(0.1)
            assert last_status != 200, (
                "removed IP was still force-selectable after the poll window"
            )

            # The surviving IPs remain fully usable.
            for ip in surviving_ips:
                status = await _request(
                    "127.0.0.1", proxy_port, upstream.url, "/t", force_ip=ip
                )
                assert status == 200
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
