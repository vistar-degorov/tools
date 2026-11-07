import json
from pathlib import Path
import subprocess
import sys


FIXTURE_DIRECTORY = Path(__file__).resolve().parents[1] / "git_grep_fixtures"
MANIFEST_PATH = FIXTURE_DIRECTORY / "manifest.json"


class GitGrepFixtureRunner:
  def __init__(self):
    manifest = json.loads(MANIFEST_PATH.read_text())
    self._cases = {
      tuple(case["command"]): case for case in manifest["cases"]
    }
    self._results = manifest["results"]

  def __call__(self, command, **kwargs):
    try:
      case = self._cases[tuple(command)]
    except KeyError:
      raise AssertionError(f"no git grep fixture for command: {command!r}") from None

    result = self._results[case["result"]]
    stdout = self._read_stream(result, "stdout")
    stderr = self._read_stream(result, "stderr")
    if stderr and kwargs.get("stderr") is not subprocess.PIPE:
      stream = getattr(sys.stderr, "buffer", sys.stderr)
      stream.write(stderr if stream is not sys.stderr else stderr.decode(errors="replace"))
      stderr = None

    return subprocess.CompletedProcess(
      command,
      result["returncode"],
      stdout if kwargs.get("stdout") is subprocess.PIPE else None,
      stderr if kwargs.get("stderr") is subprocess.PIPE else None,
    )

  def _read_stream(self, result, name):
    return (FIXTURE_DIRECTORY / result[name]["path"]).read_bytes()
