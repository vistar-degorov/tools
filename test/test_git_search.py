import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


TOOLS_DIRECTORY = Path(__file__).resolve().parents[1]
GG = TOOLS_DIRECTORY / "gg.py"
FINDDEF = TOOLS_DIRECTORY / "finddef.py"


class GitSearchTest(unittest.TestCase):
  def setUp(self):
    self.temporary_directory = tempfile.TemporaryDirectory()
    self.repository = Path(self.temporary_directory.name)
    subprocess.run(
      ("git", "init", "--quiet"), cwd=self.repository, check=True)

    self._write("a.py", """\
import os

def target():
  return os.getcwd()

def target_extra():
  return None

VALUE = 1
route = "route/123"
""")
    self._write("b.py", """\
class Target:
  pass

def target():
  return "second"
""")
    self._write("tests/test_app.py", """\
def target():
  return "test"

TEST_ONLY = True
""")
    self._write(".gitignore", "ignored.py\n")
    subprocess.run(
      ("git", "add", "."), cwd=self.repository, check=True)

    self._write("untracked.py", "class Untracked:\n  pass\n")
    self._write("ignored.py", "class Ignored:\n  pass\n")

    self.environment = os.environ.copy()
    for name in ("GG_SEARCH_IN", "GG_SEARCH_EXT", "GG_TOOL"):
      self.environment.pop(name, None)
    self.environment["GG_SEARCH_IN_DEFAULT"] = "wrong-directory"
    self.environment["GG_TOOL_DEFAULT"] = "tool-that-must-not-run"

  def tearDown(self):
    self.temporary_directory.cleanup()

  def _write(self, relative_path, contents):
    path = self.repository / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents)

  def _run(self, script, *arguments):
    return subprocess.run(
      ("python", str(script), *arguments),
      cwd=self.repository,
      env=self.environment,
      capture_output=True,
      text=True,
    )

  def test_empty_invocation_prints_usage(self):
    for script in (GG, FINDDEF):
      with self.subTest(script=script.name):
        result = self._run(script)
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

  def test_exit_codes_distinguish_matches_and_errors(self):
    self.assertEqual(self._run(GG, "target").returncode, 0)
    self.assertEqual(self._run(GG, "missing_name").returncode, 1)
    self.assertGreaterEqual(
      self._run(GG, "--bad-git-option", "target").returncode, 2)

  def test_default_scope_ignores_interactive_environment_default(self):
    result = self._run(GG, "target")
    self.assertEqual(result.returncode, 0)
    self.assertIn("a.py:", result.stdout)

  def test_default_word_search(self):
    result = self._run(GG, "target")
    self.assertEqual(result.returncode, 0)
    self.assertNotIn("target_extra", result.stdout)

  def test_import_and_test_filters(self):
    result = self._run(GG, "os")
    self.assertEqual(result.returncode, 0)
    self.assertNotIn("import os", result.stdout)
    self.assertIn("os.getcwd", result.stdout)

    result = self._run(GG, "--imports", "os")
    self.assertIn("import os", result.stdout)

    self.assertEqual(self._run(GG, "TEST_ONLY").returncode, 1)
    self.assertEqual(self._run(GG, "+t", "TEST_ONLY").returncode, 0)
    self.assertEqual(
      self._run(GG, "--tests", "only", "TEST_ONLY").returncode, 0)

  def test_separated_git_grep_option_argument(self):
    result = self._run(GG, "-A", "1", "target")
    self.assertEqual(result.returncode, 0)
    self.assertIn("target", result.stdout)

  def test_untracked_files_are_explicit(self):
    self.assertEqual(self._run(FINDDEF, "Untracked").returncode, 1)
    result = self._run(FINDDEF, "--untracked", "Untracked")
    self.assertEqual(result.returncode, 0)
    self.assertIn("untracked.py", result.stdout)
    self.assertEqual(
      self._run(FINDDEF, "--untracked", "Ignored").returncode, 1)

  def test_json_output(self):
    result = self._run(GG, "--json", "target")
    self.assertEqual(result.returncode, 0)
    records = [json.loads(line) for line in result.stdout.splitlines()]
    self.assertGreaterEqual(len(records), 2)
    self.assertEqual(
      set(records[0]), {"path", "line", "column", "text"})

  def test_human_and_strict_selection(self):
    first = self._run(FINDDEF, "target/0", "--stdout")
    self.assertEqual(first.returncode, 0)
    self.assertTrue(first.stdout.startswith("a.py:"), first.stdout)

    last = self._run(FINDDEF, "target/999", "--stdout")
    self.assertEqual(last.returncode, 0)
    self.assertTrue(last.stdout.startswith("b.py:"), last.stdout)

    strict = self._run(
      FINDDEF, "--name", "target", "--select", "999")
    self.assertEqual(strict.returncode, 2)
    self.assertIn("outside 1..", strict.stderr)

  def test_non_tty_selection_does_not_launch_editor(self):
    result = self._run(FINDDEF, "target/1")
    self.assertEqual(result.returncode, 0)
    self.assertTrue(result.stdout.startswith("a.py:"), result.stdout)

  def test_explicit_pattern_disables_slash_selection(self):
    result = self._run(GG, "--pattern", "route/123")
    self.assertEqual(result.returncode, 0)
    self.assertIn('route = "route/123"', result.stdout)


if __name__ == "__main__":
  unittest.main()
