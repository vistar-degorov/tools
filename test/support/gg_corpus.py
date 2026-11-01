from hashlib import sha256
import os
from pathlib import Path
import shutil
import subprocess


TEST_DIRECTORY = Path(__file__).resolve().parents[1]
GG_CORPUS_DIRECTORY = TEST_DIRECTORY / "corpus" / "gg"
SEARCH_CORPUS_DIRECTORY = GG_CORPUS_DIRECTORY / "search"

GIT_ENVIRONMENT_OVERRIDES = {
  "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
  "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
  "GIT_AUTHOR_NAME": "Fixture Generator",
  "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
  "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
  "GIT_COMMITTER_NAME": "Fixture Generator",
  "GIT_CONFIG_GLOBAL": os.devnull,
  "GIT_CONFIG_NOSYSTEM": "1",
  "GIT_PAGER": "cat",
  "LANG": "C",
  "LC_ALL": "C",
  "PAGER": "cat",
  "TZ": "UTC",
}


def git_environment():
  environment = os.environ.copy()
  environment.update(GIT_ENVIRONMENT_OVERRIDES)
  return environment


def copy_corpus_tree(source, destination, renames=None):
  renames = renames or {}
  for source_path in sorted(source.rglob("*")):
    if not source_path.is_file():
      continue
    relative_path = source_path.relative_to(source)
    destination_path = destination / renames.get(relative_path, relative_path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    if destination_path.exists():
      raise FileExistsError(destination_path)
    shutil.copy2(source_path, destination_path)


def corpus_digest(source=SEARCH_CORPUS_DIRECTORY):
  digest = sha256()
  for path in sorted(path for path in source.rglob("*") if path.is_file()):
    relative_path = path.relative_to(source).as_posix().encode()
    contents = path.read_bytes()
    digest.update(len(relative_path).to_bytes(8, "big"))
    digest.update(relative_path)
    digest.update(len(contents).to_bytes(8, "big"))
    digest.update(contents)
  return digest.hexdigest()


def create_gg_repository(destination, *, include_filesystem):
  destination = Path(destination)
  environment = git_environment()
  subprocess.run(
    ("git", "init", "--quiet", "--initial-branch=main"),
    cwd=destination,
    env=environment,
    check=True,
  )

  copy_corpus_tree(SEARCH_CORPUS_DIRECTORY, destination)
  if include_filesystem:
    copy_corpus_tree(
      GG_CORPUS_DIRECTORY / "filesystem" / "tracked",
      destination,
      {Path("mock-gitignore"): Path(".gitignore")},
    )

  subprocess.run(("git", "add", "."), cwd=destination, env=environment, check=True)
  subprocess.run(
    ("git", "commit", "--quiet", "-m", "fixture corpus"),
    cwd=destination,
    env=environment,
    check=True,
  )

  if include_filesystem:
    copy_corpus_tree(GG_CORPUS_DIRECTORY / "filesystem" / "untracked", destination)
    copy_corpus_tree(GG_CORPUS_DIRECTORY / "filesystem" / "ignored", destination)

  return environment
