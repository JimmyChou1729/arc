# ARC for DeepSeek Harness

This directory contains the thin DeepSeek Harness adapter for ARC. It
registers the existing ARC Skill and keeps the Skill directory as its resource
base. ARC's Python packages, runtime launcher, manuals, rules, and workflows
remain unchanged.

Install the source bundle from GitHub into a dedicated DSH profile:

```bash
dsh plugin --profile arc add github:tririver/arc
dsh --profile arc --dump-config
```

For local development, install a checkout directly:

```bash
dsh plugin --profile arc add /path/to/arc
dsh --profile arc --dump-config
```

The adapter targets Linux, macOS, and WSL because ARC's portable runtime
launcher is a Bash script. It does not add JavaScript dependencies or require
Node.js for ARC users who do not use DSH. DSH's default workspace-write
sandbox cannot create ARC's normal shared runtime below `~/.arc`; launch DSH
from a writable project directory with `ARC_HOME="$PWD/.arc"`, or configure an
equivalent writable runtime location.

## Native LLM bridge

When the bundle is active, it starts an authenticated Unix-socket bridge and
exports these trusted variables to DSH model shell commands:

- `DSH_ARC_RUNTIME`: the bundled portable ARC launcher
- `DSH_ARC_LLM_SOCKET`: the per-process local bridge socket
- `DSH_ARC_LLM_TOKEN_FILE`: the mode-0600 authentication token

ARC's `dsh` provider delegates generation to DSH's native
`ctx.llm.prepareCall().stream()` path. DSH remains responsible for provider
credentials, model routing, retries, and streaming. The bridge uses versioned
NDJSON events and does not copy credentials into ARC configuration or command
arguments.

Because `ctx.llm` is a model runtime rather than a tool-using coding agent, the
ARC provider converts its generated `host/control.json` into a self-contained
model prompt. Verified UTF-8 text, Markdown, JSON, HTML, XML, and TeX inputs
are embedded with their identity metadata. Binary inputs such as PDF and image
files are rejected explicitly; extract or render them to a supported text form
before starting the `arc-llm` request.

For example, from a DSH model shell:

```bash
"$DSH_ARC_RUNTIME" arc-llm doctor --provider dsh
```

The default route is `deepseek-official`; set `ARC_DSH_PROVIDER` to use a
different provider route already configured in DSH. ARC's default model for
the `dsh` provider is `deepseek-v4-flash`, and an explicit ARC model selection
continues to take precedence.
