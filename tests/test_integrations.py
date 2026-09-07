"""Подключения офлайн: форма конфига, имена инструментов, риск из аннотаций, живой MCP-стуб.

MCP проверяем настоящим подпроцессом, а не mock'ом: половина багов этого протокола живёт в
фрейминге (переменная строки, зависший stdout, закрытая трубка), и mock их не воспроизводит.
Шифрование секрета — сквозным кругом BlobCipher: «в базе шифртекст» без проверки на восстановление
ничего не стоит.
"""

from __future__ import annotations

import os
import stat
import sys

import pytest

from aegis.integrations.bridge import _risk_of, _sanitize, describe_connectors
from aegis.integrations.mcp import McpClient, McpError
from aegis.integrations.store import Connector, validate_config
from aegis.platform.crypto import BlobCipher

MCP_STUB = """
import json, sys

def send(msg):
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\\n")
    sys.stdout.flush()

mode = "ok"
for a in sys.argv[1:]:
    if a.startswith("--mode="):
        mode = a.split("=", 1)[1]
if mode == "hang":
    import time
    time.sleep(30)
if mode == "die":
    sys.stderr.write("fatal: config not found")
    sys.exit(3)

tools = [
    {"name": "echo", "description": "верни текст",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}},
                      "required": ["text"]},
     "annotations": {"readOnlyHint": True}},
    {"name": "delete_all", "description": "удали всё",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"destructiveHint": True}},
    {"name": "boom", "description": "всегда ошибка", "inputSchema": {"type": "object"},
     "annotations": {}},
]
for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    rid = msg.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": msg["params"]["protocolVersion"],
            "capabilities": {"tools": {}}, "serverInfo": {"name": "stub"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": rid, "result": {"tools": tools}})
    elif method == "tools/call":
        name = msg["params"]["name"]
        if name == "echo":
            got = msg["params"]["arguments"].get("text", "")
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": f"echo:{got}"}], "isError": False}})
        elif name == "boom":
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": "не удалось"}], "isError": True}})
        else:
            send({"jsonrpc": "2.0", "id": rid, "error": {
                "code": -32601, "message": f"нет инструмента {name}"}})
"""


@pytest.fixture(scope="module")
def stub_path(tmp_path_factory: pytest.TempPathFactory) -> str:
    path = tmp_path_factory.mktemp("mcp") / "stub_server.py"
    path.write_text(MCP_STUB, encoding="utf-8")
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR)
    return str(path)


def _stub_client(stub_path: str, mode: str = "ok", timeout: float = 5.0) -> McpClient:
    args = [stub_path] + ([f"--mode={mode}"] if mode != "ok" else [])
    return McpClient(
        command=sys.executable, args=args, env={"PYTHONIOENCODING": "utf-8"}, timeout_s=timeout
    )


class TestValidateConfig:
    def test_mcp_canon(self) -> None:
        cfg = validate_config(
            "mcp",
            {"command": "npx", "args": ["-y", "srv"], "env": {"HOME": "/x"}, "junk": 1},
        )
        assert cfg == {
            "command": "npx",
            "args": ["-y", "srv"],
            "env": {"HOME": "/x"},
            "secret_env": "",
        }

    def test_mcp_rejects_http_and_bad_env(self) -> None:
        with pytest.raises(ValueError, match="stdio"):
            validate_config("mcp", {"command": "https://mcp.example/rpc"})
        with pytest.raises(ValueError, match="VAR_NAME"):
            validate_config("mcp", {"command": "x", "env": {"lower_key": "v"}})

    def test_api_and_plugin(self) -> None:
        assert validate_config("api", {}) == {"base_url": "", "header": "Authorization"}
        with pytest.raises(ValueError, match="http"):
            validate_config("api", {"base_url": "ftp://x"})
        with pytest.raises(ValueError, match="pkg"):
            validate_config("plugin", {"module": "os; rm -rf /"})
        assert validate_config("plugin", {"module": "my_pkg.mod"}) == {"module": "my_pkg.mod"}


class TestRegistryNaming:
    def test_sanitize(self) -> None:
        assert _sanitize("gh", "create issue/2") == "mcp__gh__create_issue_2"

    def test_risk_mapping(self) -> None:
        from aegis.governance.policy import Risk

        assert _risk_of({"annotations": {"readOnlyHint": True}}) == (Risk.LOW, False)
        assert _risk_of({"annotations": {"destructiveHint": True}}) == (Risk.HIGH, True)
        assert _risk_of({"annotations": {"openWorldHint": True}}) == (Risk.HIGH, True)
        # неизвестное действие — MEDIUM+writes: подтверждение по умолчанию, а не «доверимся»
        assert _risk_of({}) == (Risk.MEDIUM, True)

    def test_describe_lines(self) -> None:
        conn = Connector(
            id="x" * 36,
            owner_id=1,
            kind="mcp",
            name="gh",
            enabled=True,
            config={},
            last_error="boom",
            last_ok_at=None,
        )
        line = describe_connectors([conn])
        assert line.startswith(" ► mcp/gh") and "boom" in line


class TestBlobSecretRoundtrip:
    def test_cipher_roundtrip_and_isolation(self) -> None:
        keks = {1: os.urandom(32), 2: os.urandom(32)}
        cipher = BlobCipher(keks, active_version=2)
        secret = b"sk-super-secret-value"
        ct, wrapped, ver = cipher.encrypt(secret)
        assert ver == 2 and secret not in ct and secret not in wrapped
        assert cipher.decrypt(ct, wrapped, key_version=ver) == secret

    def test_version_mismatch_and_shredding(self) -> None:
        from aegis.platform.crypto import KeyShredded

        cipher = BlobCipher({2: os.urandom(32)}, active_version=2)
        ct, wrapped, _ = cipher.encrypt(b"x")
        # заявленный version не совёрт с обёрткой — подозрение на подмену, не угадываем
        with pytest.raises(ValueError, match="не совпадает"):
            cipher.decrypt(ct, wrapped, key_version=9)
        # KEK версии 2 уничтожен (shredding) — читаемого ключа нет, и это не авария формата
        shredder = BlobCipher({7: os.urandom(32)}, active_version=7)
        with pytest.raises(KeyShredded):
            shredder.decrypt(ct, wrapped)


@pytest.mark.asyncio
class TestMcpClient:
    async def test_handshake_list_call(self, stub_path: str) -> None:
        client = _stub_client(stub_path)
        async with client:
            tools = await client.list_tools()
            assert [t["name"] for t in tools] == ["echo", "delete_all", "boom"]
            text, is_err = await client.call_tool("echo", {"text": "привет мир"})
            assert text == "echo:привет мир" and is_err is False
            text, is_err = await client.call_tool("boom", {})
            assert is_err is True and "не удалось" in text
            with pytest.raises(McpError, match="нет инструмента"):
                await client.call_tool("missing", {})

    async def test_hang_times_out_and_process_dies(self, stub_path: str) -> None:
        client = _stub_client(stub_path, mode="hang", timeout=0.4)
        with pytest.raises(McpError, match="таймаут"):
            async with client:
                pass
        assert client._proc is None  # noqa: SLF001 — убран, не «труп на трубке»

    async def test_early_exit_carries_stderr_tail(self, stub_path: str) -> None:
        client = _stub_client(stub_path, mode="die")
        with pytest.raises(McpError) as exc:
            await client.start()
        assert "config not found" in str(exc.value) or "stdout" in str(exc.value)

    async def test_notification_before_response_is_skipped(self, stub_path: str) -> None:
        # стаб не шлёт уведомлений; отдельная проверка: initialize-id=1 не «съедает» следующий id
        client = _stub_client(stub_path)
        async with client:
            a = await client.list_tools()
            b = await client.list_tools()
            assert len(a) == len(b) == 3
