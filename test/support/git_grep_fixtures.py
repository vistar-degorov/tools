import json
from pathlib import Path
import subprocess
import sys


FIXTURE_DIRECTORY = Path(__file__).resolve().parents[1] / "git_grep_fixtures"
MANIFEST_PATH = FIXTURE_DIRECTORY / "manifest.json"


class GitGrepFixtureRunner:
  def __init__(self, *case_names):
    manifest = json.loads(MANIFEST_PATH.read_text())
    cases_by_name = {case["name"]: case for case in manifest["cases"]}
    try:
      self._cases = [cases_by_name[name] for name in case_names]
    except KeyError as error:
      raise ValueError(f"unknown git grep fixture: {error.args[0]}") from None
    self._results = manifest["results"]
    self._index = 0

  def __call__(self, command, **kwargs):
    if self._index >= len(self._cases):
      raise AssertionError(f"unexpected git grep command: {command!r}")

    case = self._cases[self._index]
    self._index += 1
    expected_command = tuple(case["command"])
    if tuple(command) != expected_command:
      raise AssertionError(
        f"git grep command differs for fixture {case['name']!r}:\n"
        f"expected: {expected_command!r}\n"
        f"actual:   {tuple(command)!r}"
      )

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

  def assert_complete(self):
    if self._index != len(self._cases):
      remaining = [case["name"] for case in self._cases[self._index:]]
      raise AssertionError(f"unused git grep fixtures: {remaining!r}")
