"""Run SCT's batch CLI on this machine.

This is the host the demo uses, and the reason is not preference: SCT has to
reach the source listener, the source is a local Oracle XE behind this machine's
NAT, and nothing running inside AWS can open a connection to it without a VPN.
So for a local source, local is the only host that works -- and it costs nothing,
which makes it also the right place to develop the scenario and the parser.

**The invocation was taken from SCT's own wrapper, not from the docs.** Reading
`RunSCTBatch.cmd` in 1.0.677 settled three things guesswork had wrong:

  - the arguments are `-type scts -script <file>`, not `-s <file>`
  - eight `--add-opens` / `--add-exports` flags are required, plus `-Xss128M`
    and a raised `jdk.jar.maxSignatureFileSize`; without them SCT does not start
  - the wrapper resolves its own install path from `HKLM\\SOFTWARE\\Amazon Web
    Services, Inc.\\AWS Schema Conversion Tool`

That last point is why the jar is invoked directly rather than through the
wrapper: an administrative MSI extract (which needs no elevation, and is how a
locked-down machine gets SCT at all) writes no registry key, so the wrapper
cannot find itself. `toolchain.py` locates the jar and the JVM instead.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

from . import HostResult

NAME = "local"

# SCT on a real customer estate assesses for a long time. The default is
# generous on purpose: a timeout that kills a legitimate run wastes the whole
# run, and there is no partial report to keep.
DEFAULT_TIMEOUT_SECONDS = 4 * 60 * 60

# Copied from SCT 1.0.677's own `RunSCTBatch.cmd`. SCT will not start without
# these: it reaches into JDK internals that are closed by default since Java 9,
# and its bundled jars exceed the default signature-file cap. Keep this list in
# step with the wrapper if SCT is upgraded -- do not trim it because a run
# happens to work without one flag.
JVM_FLAGS = [
    "-Xss128M",
    "-Dfile.encoding=UTF-8",
    "-Djdk.jar.maxSignatureFileSize=50000000",
    "-XX:+UseParallelGC",
    "--add-opens=java.base/jdk.internal.loader=ALL-UNNAMED",
    "--add-exports=java.base/jdk.internal.loader=ALL-UNNAMED",
    "--add-exports=java.base/jdk.internal.misc=ALL-UNNAMED",
    "--add-opens=java.base/java.lang.reflect=ALL-UNNAMED",
    "--add-opens=java.base/java.nio=ALL-UNNAMED",
    "--add-opens=java.base/sun.security.x509=ALL-UNNAMED",
    "--add-opens=java.base/sun.security.tools.keytool=ALL-UNNAMED",
]


def run(
    scenario_path: Path,
    toolchain,
    on_line: Callable[[str], None] | None = None,
    timeout: int | None = None,
) -> HostResult:
    """Execute one scenario, streaming SCT's stdout line by line."""
    if not toolchain.ready:
        missing = [c.name for c in toolchain.checks if c.status != "ok"]
        return HostResult(
            ok=False, exit_code=None, host=NAME,
            detail="toolchain not ready: " + ", ".join(missing),
        )

    cmd = [
        str(toolchain.java),
        *JVM_FLAGS,
        "-jar", str(toolchain.sct_batch_jar),
        "-type", "scts",
        "-script", str(scenario_path),
    ]

    lines: list[str] = []
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(Path(toolchain.sct_batch_jar).parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        return HostResult(ok=False, exit_code=None, host=NAME,
                          detail=f"could not start SCT: {exc}")

    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\r\n")
            lines.append(line)
            if on_line:
                on_line(line)
        code = proc.wait(timeout=timeout or DEFAULT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        return HostResult(
            ok=False, exit_code=None, host=NAME, lines=lines,
            detail=f"SCT did not finish within {timeout or DEFAULT_TIMEOUT_SECONDS}s "
                   "and was killed; no report was produced",
        )

    return HostResult(
        ok=code == 0,
        exit_code=code,
        host=NAME,
        lines=lines,
        detail="SCT batch completed" if code == 0 else f"SCT batch exited {code}",
    )
