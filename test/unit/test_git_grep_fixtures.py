from hashlib import sha256
import json
from pathlib import Path
import unittest

from test.support.gg_corpus import corpus_digest
from test.support.git_grep_cases import GIT_GREP_CASES, validate_cases


FIXTURE_DIRECTORY = Path(__file__).resolve().parents[1] / "git_grep_fixtures"


class GitGrepFixtureUnitTest(unittest.TestCase):
  def test_cases_have_unique_names_and_commands(self):
    validate_cases()

  def test_manifest_matches_cases_corpus_and_raw_files(self):
    manifest = json.loads((FIXTURE_DIRECTORY / "manifest.json").read_text())
    self.assertEqual(manifest["schema_version"], 1)
    self.assertEqual(manifest["case_count"], len(GIT_GREP_CASES))
    self.assertEqual(manifest["corpus"]["sha256"], corpus_digest())
    self.assertEqual(
      [(case["name"], tuple(case["command"])) for case in manifest["cases"]],
      [(case.name, case.command) for case in GIT_GREP_CASES],
    )

    referenced_results = {case["result"] for case in manifest["cases"]}
    self.assertEqual(referenced_results, set(manifest["results"]))
    self.assertEqual(manifest["result_count"], len(referenced_results))
    for result in manifest["results"].values():
      for stream_name in ("stdout", "stderr"):
        metadata = result[stream_name]
        data = (FIXTURE_DIRECTORY / metadata["path"]).read_bytes()
        self.assertEqual(metadata["bytes"], len(data))
        self.assertEqual(metadata["sha256"], sha256(data).hexdigest())


if __name__ == "__main__":
  unittest.main()
