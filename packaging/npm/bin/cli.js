#!/usr/bin/env node
/*
 * npm launcher for the Python `hive` CLI.
 *
 * Hive Mind is a Python package; this npm package is a thin launcher so people who
 * live in the Node ecosystem can `npm i -g @thevinod/hive-ai` and get the `hive` command.
 * It requires Python >= 3.10 on the machine (Phase A distribution). A future phase
 * may ship standalone binaries so Python is not needed.
 *
 * Behaviour:
 *   1. find a Python >= 3.10 interpreter (python3, then python, then py -3 on Windows)
 *   2. ensure the `hive` package is importable; if not, `pip install --user hive-ai`
 *      once, with a clear message
 *   3. exec `python -m hive.cli.main <args>` forwarding stdio + exit code
 *
 * It never bundles or vendors Python. Failures print actionable guidance.
 */
"use strict";

const { spawnSync } = require("child_process");

const PKG = "hive-ai";          // PyPI distribution name
const MODULE = "hive.cli.main";   // python -m target
const MIN = [3, 10];

function tryVersion(cmd, args) {
  // Returns [major, minor] if `cmd` runs and reports a version, else null.
  const probe = spawnSync(
    cmd,
    [...args, "-c", "import sys;print('%d %d' % sys.version_info[:2])"],
    { encoding: "utf8" }
  );
  if (probe.status !== 0 || !probe.stdout) return null;
  const m = probe.stdout.trim().split(/\s+/).map(Number);
  return m.length === 2 && !m.some(isNaN) ? m : null;
}

function findPython() {
  const candidates =
    process.platform === "win32"
      ? [["py", ["-3"]], ["python", []], ["python3", []]]
      : [["python3", []], ["python", []]];
  for (const [cmd, args] of candidates) {
    const v = tryVersion(cmd, args);
    if (v && (v[0] > MIN[0] || (v[0] === MIN[0] && v[1] >= MIN[1]))) {
      return { cmd, prefix: args, version: v };
    }
  }
  return null;
}

function hiveImportable(py) {
  return (
    spawnSync(py.cmd, [...py.prefix, "-c", "import hive"], { stdio: "ignore" })
      .status === 0
  );
}

function ensureInstalled(py) {
  if (hiveImportable(py)) return true;
  process.stderr.write(
    `[hive] Python package not found — installing ${PKG} via pip (one time)...\n`
  );
  const install = spawnSync(
    py.cmd,
    [...py.prefix, "-m", "pip", "install", "--user", PKG],
    { stdio: "inherit" }
  );
  if (install.status !== 0 || !hiveImportable(py)) {
    process.stderr.write(
      `\n[hive] Could not install ${PKG} automatically.\n` +
        `       Install it yourself, then re-run:\n` +
        `         ${py.cmd} ${py.prefix.join(" ")} -m pip install ${PKG}\n` +
        `       or use pipx:  pipx install ${PKG}\n`
    );
    return false;
  }
  return true;
}

function main() {
  const py = findPython();
  if (!py) {
    process.stderr.write(
      "[hive] Python >= 3.10 is required but was not found on PATH.\n" +
        "       Install Python 3.10+ (https://www.python.org/downloads/) and retry.\n"
    );
    process.exit(1);
  }
  if (!ensureInstalled(py)) process.exit(1);

  const run = spawnSync(
    py.cmd,
    [...py.prefix, "-m", MODULE, ...process.argv.slice(2)],
    { stdio: "inherit" }
  );
  process.exit(run.status === null ? 1 : run.status);
}

main();
