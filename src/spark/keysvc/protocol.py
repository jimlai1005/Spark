"""src/spark/keysvc/protocol.py
key-service 的 socket 協定：newline 結尾的 JSON。唯一操作 generate。
私鑰絕不出現在任何訊息——回應只帶 agent 地址或錯誤。"""
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class GenerateRequest:
    account_id: str


@dataclass(frozen=True)
class Response:
    ok: bool
    agent_address: str | None = None
    error: str | None = None


def encode_request(req: GenerateRequest) -> bytes:
    return (json.dumps({"op": "generate", "account_id": req.account_id}) + "\n").encode()


def decode_request(line: bytes) -> GenerateRequest:
    d = json.loads(line.decode())
    if d.get("op") != "generate":
        raise ValueError(f"unsupported op: {d.get('op')!r}")
    acct = d.get("account_id")
    if not acct:
        raise ValueError("missing account_id")
    return GenerateRequest(account_id=acct)


def encode_response(resp: Response) -> bytes:
    body = {"ok": resp.ok}
    if resp.agent_address is not None:
        body["agent_address"] = resp.agent_address
    if resp.error is not None:
        body["error"] = resp.error
    return (json.dumps(body) + "\n").encode()


def decode_response(line: bytes) -> Response:
    d = json.loads(line.decode())
    return Response(ok=bool(d.get("ok")), agent_address=d.get("agent_address"),
                     error=d.get("error"))
