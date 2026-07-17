"""src/spark/keysvc/protocol.py
key-service 的 socket 協定：newline 結尾的 JSON。兩個操作：generate（寫）、
address（唯讀，desync 自癒用，設計定案 12）。
私鑰絕不出現在任何訊息——回應只帶 agent 地址或錯誤。"""
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class GenerateRequest:
    account_id: str


@dataclass(frozen=True)
class AddressRequest:
    account_id: str


@dataclass(frozen=True)
class Response:
    ok: bool
    agent_address: str | None = None
    error: str | None = None
    code: str | None = None  # 結構化錯誤碼："exists"|"invalid"|"missing"|"internal"；成功 None


def encode_request(req: GenerateRequest | AddressRequest) -> bytes:
    op = "generate" if isinstance(req, GenerateRequest) else "address"
    return (json.dumps({"op": op, "account_id": req.account_id}) + "\n").encode()


def decode_request(line: bytes) -> GenerateRequest | AddressRequest:
    d = json.loads(line.decode())
    if not isinstance(d, dict):
        raise ValueError("request must be a JSON object")
    acct = d.get("account_id")
    if not acct:
        raise ValueError("missing account_id")
    op = d.get("op")
    if op == "generate":
        return GenerateRequest(account_id=acct)
    if op == "address":
        return AddressRequest(account_id=acct)
    raise ValueError(f"unsupported op: {op!r}")


def encode_response(resp: Response) -> bytes:
    body = {"ok": resp.ok}
    if resp.agent_address is not None:
        body["agent_address"] = resp.agent_address
    if resp.error is not None:
        body["error"] = resp.error
    if resp.code is not None:
        body["code"] = resp.code
    return (json.dumps(body) + "\n").encode()


def decode_response(line: bytes) -> Response:
    d = json.loads(line.decode())
    return Response(ok=bool(d.get("ok")), agent_address=d.get("agent_address"),
                     error=d.get("error"), code=d.get("code"))
