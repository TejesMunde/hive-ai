# hive-mind

npm launcher for the [Hive Mind](https://github.com/TejesMunde/hive-ai) Python package — persistent, cross-agent memory for AI coding agents.

## Prerequisites

**Python >= 3.10** must be on the machine. The npm package is a thin launcher that finds your Python interpreter and runs the `hive` CLI.

## Install

```bash
npm install -g hive-mind
hive --help
```

On first run, it auto-installs the Python package via `pip install --user hive-mind`.

## Usage

```bash
hive recall   <project> "<query>"          # retrieve ranked context
hive remember <project> "<what>" "<why>"   # record a decision
hive capture  <sha>                        # extract decisions from git commits
hive staging  list                         # review guard-flagged writes
hive audit    tail                         # append-only event log
```

See the [full documentation](https://github.com/TejesMunde/hive-ai) on GitHub.
