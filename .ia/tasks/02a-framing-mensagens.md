# T02a — framing e validação de mensagens do protocolo v1

## Objetivo
Implementar a leitura/escrita de mensagens e a validação do envelope e dos bodies do protocolo Nexus v1. Python 3.12, SÓ stdlib. Sem rede de verdade nesta tarefa: as funções recebem objetos tipo arquivo/socket.

## Arquivos (crie só estes)
- `nexus/proto/__init__.py` (vazio)
- `nexus/proto/framing.py`
- `nexus/proto/messages.py`
- `tests/test_framing.py`
- `tests/test_messages.py`

## nexus/proto/framing.py
```python
MAX_FRAME = 1024 * 1024
class FrameError(Exception): ...
def encode(msg: dict) -> bytes      # json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8"); se len > MAX_FRAME -> FrameError; retorna struct.pack(">I", n) + payload
def read_frame(recv_exact) -> dict  # recv_exact(n) -> bytes com exatamente n bytes (ou levanta EOFError)
```
`read_frame`: lê 4 bytes, `n = struct.unpack(">I", ...)`. Se `n == 0` ou `n > MAX_FRAME` → `FrameError` (SEM ler o payload). Lê n bytes, decodifica UTF-8 (erro → `FrameError`), `json.loads` (erro → `FrameError`); se o resultado não for `dict` → `FrameError`.
Também `def recv_exact_from(sock)`: devolve uma função `recv_exact(n)` que chama `sock.recv` em loop até juntar n bytes; `recv` retornando `b""` → `EOFError`.

## nexus/proto/messages.py
```python
class ProtocolError(Exception):
    def __init__(self, code: str, message: str): ...   # guarda .code e .message
SCHEMAS: dict[str, dict[str, type | tuple]]
def validate(msg: dict) -> dict      # devolve msg se válido; senão ProtocolError
def make(type_: str, body: dict, re: str | None = None) -> dict   # gera envelope com v=1, id=str(uuid.uuid4()); valida antes de retornar
```
Envelope: chaves permitidas `v`, `id`, `type`, `body`, `re`. Regras de `validate`:
- chave extra no envelope → `ProtocolError("bad_envelope", ...)`
- `v` precisa ser exatamente `int` 1 (bool não vale) → senão `"bad_version"`
- `id`: str de 1 a 64 caracteres → senão `"bad_envelope"`
- `re` opcional: str de 1 a 64 caracteres
- `type` precisa estar em `SCHEMAS` → senão `"unknown_type"`
- `body`: dict. Cada chave do schema é obrigatória, salvo se o nome terminar com `?` no schema (opcional). Chave do body fora do schema → `"bad_body"`. Tipo errado → `"bad_body"`. Use `isinstance`, mas `bool` NÃO conta como `int`.
- strings: nenhuma str do body pode passar de 65536 caracteres → `"too_large"`.

SCHEMAS (exatamente):
```
"hello":          {"device_id": str, "name": str, "kind": str, "app_version": str, "protocol": list, "cert_pem?": str}
"pair.request":   {"commit": str}
"pair.challenge": {"session": str, "n_s": str, "expires_in": int}
"pair.reveal":    {"session": str, "n_c": str}
"pair.confirm":   {"session": str, "accept": bool}
"pair.result":    {"ok": bool, "reason?": str}
"unpair":         {}
"ping":           {}
"pong":           {}
"actions.list":   {}
"actions.result": {"actions": list}
"action.run":     {"action": str, "params": dict}
"action.result":  {"ok": bool, "result?": (str, int, float, bool, list, dict, type(None)), "error?": str}
"share.text":     {"text": str}
"clipboard.set":  {"text": str}
"error":          {"code": str, "message": str}
```
Validações extras dentro de `validate`:
- `hello.kind` ∈ {"phone", "tablet", "pc"}
- `pair.request.commit`, `pair.challenge.n_s`, `pair.reveal.n_c`: exatamente 64 caracteres hex minúsculos (regex `^[0-9a-f]{64}$`) → senão `"bad_body"`
- `hello.device_id`: mesma regex de 64 hex
- `action.run.params`: chaves str; valores só str/int/bool (não aninhados) → senão `"bad_body"`

## Testes (unittest)
- framing: ida e volta encode → read_frame (use `io.BytesIO(...).read` embrulhado numa função que levanta EOFError se vier curto); frame de tamanho 0; tamanho > MAX_FRAME (só o header, sem payload: deve falhar sem tentar ler); payload não UTF-8; JSON inválido; JSON lista em vez de objeto; EOF no meio; `recv_exact_from` com um socket falso que entrega 1 byte por chamada.
- messages: uma mensagem válida de cada tipo; chave extra no envelope; v=True; v=2; id vazio; type desconhecido; body com chave extra; chave obrigatória faltando; opcional ausente (ok); bool passado onde se espera int (`expires_in: True` → erro); commit com maiúsculas → erro; kind inválido; params aninhado → erro; string > 65536 → `too_large`; `make` gera id único e valida.

## Restrições
Não toque em outros arquivos. Sem dependências externas. Código mínimo.

## Aceite
```
python3 -m unittest tests.test_framing tests.test_messages -v
```
