# compose-mind

An AI-assisted CLI for operating Docker Compose stacks. Describe what you want
in plain language — *"why is the api slow?"*, *"restart the worker"*,
*"scale web to 3"* — and compose-mind inspects the running stack, reasons about
it with Claude, and executes the right Docker operations behind a layer of
safety guardrails.

> ⚠️ **Status: early development.** The Docker and guardrails layers are
> implemented and tested; the agent loop, CLI, output, and history layers are
> still being wired up. See [Project status](#project-status).

## How it works

```
            ┌──────────────┐   tool calls   ┌─────────────┐
  you  ──▶  │  agent loop  │ ─────────────▶ │ guardrails  │
            │  (Claude)    │ ◀───────────── │  (risk gate)│
            └──────────────┘    results     └──────┬──────┘
                   ▲                                │ approved
                   │ stack context                  ▼
            ┌──────┴───────┐                 ┌─────────────┐
            │ docker.compose│                │ docker.*    │
            │  (parse yaml) │                │ inspect /   │
            └───────────────┘                │ control     │
                                             └─────────────┘
```

1. The compose file is parsed into structured context for the model.
2. Claude decides which tools to call (inspect state, read logs, restart, scale…).
3. Every mutating tool call is classified by **risk** and gated before it runs.
4. High-risk actions require explicit confirmation; destructive ones are blocked outright.

## Safety model

Every operation is classified into one of four levels before execution:

| Level | Behavior | Examples |
|-------|----------|----------|
| **SAFE** | Runs immediately, no prompt | all diagnostics, scaling **up** |
| **WARN** | Yellow warning + `y/N` | restart, scale **down**, `exec` |
| **CONFIRM_BY_NAME** | Must type the exact service/project name | `stop_service`, `stop_all` (<4 services) |
| **BLOCKED** | Refused | `stop_all` on 4+ services |

**Hard blocks** are refused immediately, with no prompt:

- Any unknown / unregistered tool
- `exec` commands containing `rm -rf`, `DROP TABLE`, `DELETE FROM`, or `truncate`
- `stop_all` when a database service (`postgres` / `mysql` / `mongo`) has volumes attached

## Installation

Requires Python 3.9+ and a reachable Docker daemon (Docker Desktop or `dockerd`).

```bash
git clone <repo-url>
cd compose_mind

python -m venv .venv && source .venv/bin/activate
pip install -e .
```

Set your Anthropic API key:

```bash
cp .env.example .env
# edit .env and set ANTHROPIC_API_KEY=sk-ant-...
```

## Usage

Run from a directory containing a `docker-compose.yml`:

```bash
compose-mind "why is the api container restarting?"
compose-mind "show me error logs for web"
compose-mind "scale the worker to 3 replicas"
compose-mind "restart redis"
```

compose-mind auto-detects the compose file (`docker-compose.yml`,
`docker-compose.yaml`, `compose.yml`, or `compose.yaml`) and uses the current
directory name as the compose project name.

## Project layout

```
compose_mind/
├── cli.py              # Typer entry point (compose-mind command)
├── state.py            # shared runtime state
├── output.py           # rich rendering
├── agent/
│   ├── loop.py         # the Claude reasoning loop
│   ├── tools.py        # tool registry + schemas (source of truth for TOOL_NAMES)
│   └── prompts.py      # system / task prompts
├── docker/
│   ├── compose.py      # parse docker-compose.yml → ComposeConfig
│   ├── inspect.py      # read-only: health, stats, logs
│   └── control.py      # mutations: restart, scale, stop, exec
├── guardrails/
│   └── guard.py        # risk classification + confirmation gates
└── history/
    └── db.py           # persistence of past runs
```

## Project status

| Module | State |
|--------|-------|
| `docker/compose.py` | ✅ implemented & tested |
| `docker/inspect.py` | ✅ implemented & tested |
| `docker/control.py` | ✅ implemented & tested |
| `guardrails/guard.py` | ✅ implemented & tested |
| `agent/tools.py` | 🟡 registry done, schemas pending |
| `agent/loop.py` | ⬜ scaffolded |
| `agent/prompts.py` | ⬜ scaffolded |
| `cli.py` / `output.py` / `state.py` | ⬜ scaffolded |
| `history/db.py` | ⬜ scaffolded |

## Development

```bash
pip install -r requirements.txt
```

The package is installed in editable mode, so the `compose-mind` command
reflects local changes. Modules are designed to be imported as
`compose_mind.<subpackage>.<module>` — note the package directory is named
`docker`, so always import via the `compose_mind.` prefix to avoid shadowing the
Docker SDK.

## License

[MIT](LICENSE) © 2026 Sharwari Akre
