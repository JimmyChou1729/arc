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
