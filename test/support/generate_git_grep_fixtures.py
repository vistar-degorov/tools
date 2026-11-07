from hashlib import sha256
import json
from pathlib import Path
import subprocess
import tempfile

from .gg_corpus import GIT_ENVIRONMENT_OVERRIDES, SEARCH_CORPUS_DIRECTORY, corpus_digest, create_gg_repository
from .git_grep_cases import GIT_GREP_CASES, validate_cases


FIXTURE_DIRECTORY = Path(__file__).resolve().parents[1] / "git_grep_fixtures"
RESULT_DIRECTORY = FIXTURE_DIRECTORY / "results"
MANIFEST_PATH = FIXTURE_DIRECTORY / "manifest.json"


def _command_output(command, *, cwd, environment):
  return subprocess.run(
    command,
    cwd=cwd,
    env=environment,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
  )


def _result_id(returncode, stdout, stderr):
  digest = sha256()
  digest.update(str(returncode).encode())
  for data in (stdout, stderr):
    digest.update(len(data).to_bytes(8, "big"))
    digest.update(data)
  return digest.hexdigest()[:16]


def _file_metadata(path, data):
  return {
    "path": path.relative_to(FIXTURE_DIRECTORY).as_posix(),
    "bytes": len(data),
    "sha256": sha256(data).hexdigest(),
  }


def _git_metadata(environment):
  version = _command_output(("git", "version"), cwd=SEARCH_CORPUS_DIRECTORY, environment=environment)
  build_options = _command_output(("git", "version", "--build-options"), cwd=SEARCH_CORPUS_DIRECTORY, environment=environment)
  if version.returncode != 0 or build_options.returncode != 0:
    raise RuntimeError("could not determine git version")
  return {
    "version": version.stdout.decode(errors="replace").strip(),
    "build_options": build_options.stdout.decode(errors="replace").splitlines(),
  }


def generate_fixtures():
  validate_cases()
  FIXTURE_DIRECTORY.mkdir(parents=True, exist_ok=True)
  RESULT_DIRECTORY.mkdir(parents=True, exist_ok=True)

  with tempfile.TemporaryDirectory() as temporary_directory:
    repository = Path(temporary_directory)
    environment = create_gg_repository(repository, include_filesystem=False)
    git_metadata = _git_metadata(environment)
    head = _command_output(("git", "rev-parse", "HEAD"), cwd=repository, environment=environment)
    if head.returncode != 0:
      raise RuntimeError("could not resolve fixture repository HEAD")

    cases = []
    results = {}
    result_data = {}
    for case in GIT_GREP_CASES:
      completed = _command_output(case.command, cwd=repository, environment=environment)
      result_id = _result_id(completed.returncode, completed.stdout, completed.stderr)
      previous = result_data.setdefault(
        result_id,
        (completed.returncode, completed.stdout, completed.stderr),
      )
      if previous != (completed.returncode, completed.stdout, completed.stderr):
        raise RuntimeError(f"fixture result hash collision: {result_id}")

      cases.append({
        "name": case.name,
        "command": list(case.command),
        "mode": case.mode,
        "result": result_id,
      })

    expected_files = set()
    for result_id, (returncode, stdout, stderr) in sorted(result_data.items()):
      stdout_path = RESULT_DIRECTORY / f"{result_id}.stdout"
      stderr_path = RESULT_DIRECTORY / f"{result_id}.stderr"
      stdout_path.write_bytes(stdout)
      stderr_path.write_bytes(stderr)
      expected_files.update((stdout_path, stderr_path))
      results[result_id] = {
        "returncode": returncode,
        "stdout": _file_metadata(stdout_path, stdout),
        "stderr": _file_metadata(stderr_path, stderr),
      }

    for path in RESULT_DIRECTORY.iterdir():
      if path.is_file() and path.suffix in (".stdout", ".stderr") and path not in expected_files:
        path.unlink()

    manifest = {
      "schema_version": 1,
      "generator": "python -m test.support.generate_git_grep_fixtures",
      "git": git_metadata,
      "environment": dict(sorted(GIT_ENVIRONMENT_OVERRIDES.items())),
      "corpus": {
        "path": SEARCH_CORPUS_DIRECTORY.relative_to(Path(__file__).resolve().parents[2]).as_posix(),
        "sha256": corpus_digest(),
        "repository_head": head.stdout.decode().strip(),
      },
      "case_count": len(cases),
      "result_count": len(results),
      "cases": cases,
      "results": results,
    }
    temporary_manifest = MANIFEST_PATH.with_suffix(".json.tmp")
    temporary_manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    temporary_manifest.replace(MANIFEST_PATH)

  return manifest


def main():
  manifest = generate_fixtures()
  print(
    f"generated {manifest['case_count']} cases with "
    f"{manifest['result_count']} distinct results using {manifest['git']['version']}"
  )


if __name__ == "__main__":
  main()
