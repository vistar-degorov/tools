import errno
import json
import os
from pathlib import Path
import pty
import subprocess
import tempfile
import unittest

from git_search import remove_color_codes


TOOLS_DIRECTORY = Path(__file__).resolve().parents[1]
GG = TOOLS_DIRECTORY / "gg.py"
FINDDEF = TOOLS_DIRECTORY / "finddef.py"
OPEN_RESULT = TOOLS_DIRECTORY / "open_result.py"


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
    self._write("nested/scoped.py", """\
def scoped():
  return True
""")
    self._write("frontend/index.tsx", """\
export const featureToggle = true
""")
    self._write("frontend/routes/api.ts", """\
export const endpointPath = "api/status"
""")
    self._write("backend/routes/api.ts", """\
export const endpointPath = "api/status"
""")
    self._write(".gitignore", "ignored.py\n")
    subprocess.run(
      ("git", "add", "."), cwd=self.repository, check=True)

    self._write("untracked.py", "class Untracked:\n  pass\n")
    self._write("ignored.py", "class Ignored:\n  pass\n")

    self.environment = os.environ.copy()
    for name in (
      "GG_SEARCH_IN", "GG_SEARCH_IN_DEFAULT",
      "GG_SEARCH_EXT", "GG_SEARCH_EXT_DEFAULT",
      "GG_TOOL", "GG_TOOL_DEFAULT",
    ):
      self.environment.pop(name, None)
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

  def _run_tty(self, script, *arguments):
    command = ("python", str(script), *arguments)
    master, slave = pty.openpty()
    try:
      process = subprocess.Popen(
        command,
        cwd=self.repository,
        env=self.environment,
        stdout=slave,
        stderr=subprocess.PIPE,
      )
    finally:
      os.close(slave)

    chunks = []
    try:
      while True:
        try:
          chunk = os.read(master, 4096)
        except OSError as error:
          if error.errno == errno.EIO:
            break
          raise
        if not chunk:
          break
        chunks.append(chunk)
    finally:
      os.close(master)

    _, stderr = process.communicate()
    return subprocess.CompletedProcess(
      command,
      process.returncode,
      b"".join(chunks).decode(errors="replace"),
      stderr.decode(errors="replace"),
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

  def test_default_scope_is_independent_of_output_destination(self):
    self.environment["GG_SEARCH_IN_DEFAULT"] = "nested/"
    piped = self._run(GG, "scoped")
    terminal = self._run_tty(GG, "scoped")
    self.assertEqual(piped.returncode, 0)
    self.assertEqual(terminal.returncode, 0)
    self.assertIn("nested/scoped.py:", piped.stdout)
    self.assertIn("nested/scoped.py:", remove_color_codes(terminal.stdout))

  def test_terminal_output_is_colored(self):
    result = self._run_tty(GG, "target")
    self.assertEqual(result.returncode, 0)
    self.assertIn("\x1b[", result.stdout)

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

  def test_pattern_options_can_be_repeated_and_mixed(self):
    forms = (
      ("--pattern", "AlphaMarker", "--pattern", "BetaMarker"),
      ("--pattern", "AlphaMarker", "-e", "BetaMarker"),
      ("-e", "AlphaMarker", "--pattern", "BetaMarker"),
      ("-e", "AlphaMarker", "-e", "BetaMarker"),
    )
    for arguments in forms:
      with self.subTest(arguments=arguments):
        result = self._run(GG, "+print", *arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        command = shlex.split(result.stdout)
        patterns = [
          command[index + 1]
          for index, argument in enumerate(command[:-1])
          if argument == "-e"
        ]
        self.assertEqual(patterns, ["AlphaMarker", "BetaMarker"])

    result = self._run(
      GG, "--pattern", "AlphaMarker", "-e", "BetaMarker")
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertIn("a.py:", result.stdout)
    self.assertIn("b.py:", result.stdout)

  def test_repeated_patterns_support_trailing_result_selection(self):
    result = self._run(
      GG, "--stdout",
      "--pattern", "AlphaMarker", "--pattern", "BetaMarker",
      "a.py", "b.py", "/2",
    )
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertTrue(result.stdout.startswith("b.py:"), result.stdout)

  def test_paths_with_optional_separator_override_default_scope_and_extensions(self):
    self.environment["GG_SEARCH_IN_DEFAULT"] = "nested/"
    for separator in ((), ("--",)):
      with self.subTest(separator=separator):
        result = self._run(
          GG, "-n", "featureToggle", *separator,
          "frontend/index.tsx", "frontend/routes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("frontend/index.tsx:", result.stdout)

        result = self._run(
          GG, "--no-word", "--pattern", "api/status", *separator,
          "frontend", "backend")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("frontend/routes/api.ts:", result.stdout)
        self.assertIn("backend/routes/api.ts:", result.stdout)

  def test_explicit_paths_still_exclude_tests_by_default(self):
    self.assertEqual(
      self._run(GG, "TEST_ONLY", "--", "tests").returncode, 1)
    self.assertEqual(
      self._run(GG, "+t", "TEST_ONLY", "--", "tests").returncode, 0)

  def test_finddef_accepts_paths_after_separator(self):
    self.environment["GG_SEARCH_IN_DEFAULT"] = "nested/"
    result = self._run(FINDDEF, "target", "--", "b.py")
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertIn("b.py:", result.stdout)

  def test_misplaced_paths_get_an_actionable_hint(self):
    result = self._run(GG, "-n", "target", "a.py")
    self.assertEqual(result.returncode, 128)
    self.assertIn("if any arguments are paths", result.stderr)

  def test_help_explains_paths_and_effective_defaults(self):
    self.environment["GG_SEARCH_IN_DEFAULT"] = "nested/"
    self.environment["GG_SEARCH_EXT_DEFAULT"] = ".ts .tsx"
    result = self._run(GG, "--help")
    self.assertEqual(result.returncode, 0)
    help_text = " ".join(result.stdout.split())
    self.assertIn("[-- PATH ...]", help_text)
    self.assertIn("Paths go after --", help_text)
    self.assertIn("effective default: nested/", help_text)
    self.assertIn("effective default: .ts .tsx", help_text)
    self.assertIn("line numbers by default", help_text)

  def test_explicit_path_options_are_not_silently_ignored(self):
    for option in (("--root", "nested/"), ("--ext", "py")):
      with self.subTest(option=option[0]):
        result = self._run(GG, *option, "target", "--", "a.py")
        self.assertEqual(result.returncode, 2)
        self.assertIn("cannot be combined", result.stderr)

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
      FINDDEF, "--name", "target", "--show", "999")
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

  def test_open_result_uses_shared_selection_and_display(self):
    environment = self.environment.copy()
    environment.pop("GG_TOOL_DEFAULT")
    result = subprocess.run(
      ("python", str(OPEN_RESULT), "999"),
      cwd=self.repository,
      env=environment,
      input="a.py:3:def target():\nb.py:4:def target():\n",
      capture_output=True,
      text=True,
    )
    self.assertEqual(result.returncode, 0)
    self.assertTrue(result.stdout.startswith("b.py:4:0\n"), result.stdout)


if __name__ == "__main__":
  unittest.main()
