# T01 — núcleo: catálogo de ações, CLI e log

## Objetivo
Criar o pacote Python `nexus` com um catálogo declarativo de ações, uma CLI (`python3 -m nexus`) e um log JSONL. Python 3.12, SÓ biblioteca padrão. Sem dependências externas.

## Arquivos (crie exatamente estes, nada mais)
- `nexus/__init__.py` — só `__version__ = "0.1.0"`
- `nexus/__main__.py` — `from nexus.cli import main; raise SystemExit(main())`
- `nexus/catalog.py`
- `nexus/log.py`
- `nexus/cli.py`
- `nexus/actions/__init__.py` — função `load_all()` que importa `nexus.actions.core` (import tardio dentro da função).
- `nexus/actions/core.py`
- `tests/__init__.py` (vazio)
- `tests/test_catalog.py`
- `tests/test_cli.py`

## nexus/catalog.py
```python
@dataclass(frozen=True)
class Param:
    name: str
    type: str = "str"          # um de: "str", "int", "bool", "path"
    required: bool = True
    default: object = None
    help: str = ""

@dataclass(frozen=True)
class Action:
    id: str                     # regex ^[a-z][a-z0-9-]*(\.[a-z][a-z0-9-]*)+$  ex.: "core.echo"
    description: str
    handler: Callable[[dict], object]
    params: tuple[Param, ...] = ()
    precondition: Callable[[], str | None] | None = None   # None = ok; string = motivo da falha
    dangerous: bool = False     # exige confirmação explícita
    phone_safe: bool = False    # pode ser exposta como atalho no telefone
    timeout: float = 30.0
```
Exceções próprias: `class ActionError(Exception)` e as subclasses `UnknownAction`, `InvalidParams`, `PreconditionFailed`, `NeedsConfirmation`, `ActionTimeout`.

Funções:
- `REGISTRY: dict[str, Action] = {}`
- `register(action) -> Action`: levanta `ValueError` se o id não casar com a regex, se já existir, ou se `phone_safe and dangerous`.
- `get(action_id) -> Action`: levanta `UnknownAction`.
- `all_actions() -> list[Action]` ordenada por id.
- `parse_params(action, pairs: list[str]) -> dict`: cada item é `k=v` (divide no PRIMEIRO `=`). Sem `=` → `InvalidParams`. Chave desconhecida → `InvalidParams`. Chave repetida → `InvalidParams`. Faltando um obrigatório → `InvalidParams`. Opcional ausente → usa `default`. Conversão: `int` via `int()` (erro → `InvalidParams`); `bool` aceita `true/false/1/0/yes/no` (case-insensitive), outro valor → `InvalidParams`; `path` → `Path(v).expanduser()`, e se não existir → `InvalidParams`; `str` fica como está.
- `run(action_id, params: dict, *, confirm=False) -> object`: na ordem: `get`; se `dangerous` e não `confirm` → `NeedsConfirmation`; se `precondition` devolver string → `PreconditionFailed(motivo)`; executa `handler(params)` num `concurrent.futures.ThreadPoolExecutor(max_workers=1)` com `result(timeout=action.timeout)`; estouro → `ActionTimeout`. Use `executor.shutdown(wait=False)` para não travar no timeout. SEMPRE chama `nexus.log.log_event(...)` no fim (num `finally`) com: `action` (id), `ok` (bool), `error` (nome da classe da exceção ou None), `duration_ms` (int), `params` = LISTA ORDENADA DAS CHAVES (nunca os valores, por privacidade). Exceção do handler é repassada sem alteração.

## nexus/log.py
- `state_dir() -> Path`: `$NEXUS_STATE_DIR` se definido; senão `$XDG_STATE_HOME/nexus`; senão `~/.local/state/nexus`. Cria com `mkdir(parents=True, exist_ok=True)`.
- `log_event(event: dict) -> None`: acrescenta uma linha JSON (`ensure_ascii=False`) em `state_dir()/events.jsonl`, adicionando `ts` = `datetime.now(timezone.utc).isoformat()`. Falha de IO NÃO pode derrubar a ação: capture `OSError` e escreva o aviso em stderr.

## nexus/actions/core.py
Registra, ao ser importado:
- `core.echo`: param `text` (str, obrigatório); devolve `params["text"]`. `phone_safe=False`.
- `core.version`: sem params; devolve `nexus.__version__`.

## nexus/cli.py
`main(argv=None) -> int` com argparse, `prog="nexus"`. Chama `nexus.actions.load_all()` no início.
- `nexus actions [--json]`: lista as ações. Texto: uma linha por ação, `id  descrição`, e embaixo de cada uma os params como `    nome:tipo` (com `?` no fim se for opcional). `--json`: lista de objetos `{id, description, dangerous, phone_safe, params:[{name,type,required,default,help}]}`.
- `nexus run <id> [k=v ...] [--yes]`: `parse_params` e depois `run(..., confirm=args.yes)`. Resultado `str` é impresso como está; `None` não imprime nada; qualquer outro valor vira `json.dumps(..., ensure_ascii=False, default=str)`.
- Códigos de saída: 0 ok; 2 `UnknownAction`/`InvalidParams`; 3 `PreconditionFailed`; 4 `NeedsConfirmation`; 124 `ActionTimeout`; 1 qualquer outra exceção. Erros vão para stderr como `nexus: <mensagem>`.

## Testes (unittest, sem pytest)
Cada teste usa `NEXUS_STATE_DIR` num `tempfile.TemporaryDirectory()` (via `unittest.mock.patch.dict(os.environ, ...)`). Ações de teste são registradas com ids únicos `test.*` e removidas de `REGISTRY` no `tearDown`.
- `test_catalog.py`: id inválido; id duplicado; `phone_safe`+`dangerous` rejeitado; cada regra de `parse_params` (sem `=`, desconhecida, repetida, faltando, default, int, bool válido e inválido, path inexistente, valor com `=` dentro); `dangerous` sem confirm → `NeedsConfirmation`; precondition falhando; timeout (handler com `time.sleep(1)`, `timeout=0.1`) → `ActionTimeout`; o log grava as CHAVES e NÃO grava o valor (escreva um valor "segredo123" e confira que ele não aparece no arquivo).
- `test_cli.py`: chame `main([...])` capturando stdout/stderr com `contextlib.redirect_stdout/redirect_stderr`; `run core.echo text=oi` → saída "oi" e código 0; ação desconhecida → 2; `actions --json` é JSON válido e contém `core.echo`.

## Restrições
- Não crie outros arquivos, setup.py, pyproject nem requirements.
- Sem `shell=True`, sem `eval`/`exec`.
- Código mínimo; sem classes ou camadas além do pedido.

## Aceite (rode antes de terminar)
```
python3 -m unittest discover -s tests -v && python3 -m nexus actions && python3 -m nexus run core.echo text=oi
```
