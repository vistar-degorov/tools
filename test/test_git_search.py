import errno
import json
import os
from pathlib import Path
import pty
import shlex
import subprocess
import tempfile
import unittest

from git_search import remove_color_codes, SearchResult, GrepOutputMode
from git_search_args import make_output_plan, prepare_output, without_context_options
from gg import _is_import


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
AlphaMarker = object()
""")
    self._write("b.py", """\
class Target:
  pass

def target():
  return "second"

BetaMarker = object()
""")
    self._write("dependencies.py", "import decimal\n")
    self._write("tests/test_app.py", """\
def target():
  return "test"

TEST_ONLY = True
""")
    self._write("nested/scoped.py", """\
def scoped():
  return True
""")
    self._write("import directory/plain.py", """\
PATH_MARKER = True
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
    subprocess.run(
      (
        "git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
        "commit", "--quiet", "-m", "initial",
      ),
      cwd=self.repository,
      check=True,
    )

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

  def test_import_filter_only_checks_matched_text(self):
    result = self._run(GG, "PATH_MARKER")
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertIn("import directory/plain.py:", result.stdout)

  def test_import_filter_ignores_color_codes(self):
    match = SearchResult(
      "module.py", 1, 1, "\x1b[1;31mimport\x1b[m dependency")
    occurrence = SearchResult("module.py", 1, 8, "dependency")
    self.assertTrue(_is_import(match, (occurrence,)))

  def test_occurrence_import_filter_keeps_mixed_lines(self):
    match = SearchResult(
      "module.py", 1, 1, "import package; package.run()")
    imported = SearchResult("module.py", 1, 8, "package")
    used = SearchResult("module.py", 1, 17, "package")
    self.assertTrue(_is_import(match, (imported,)))
    self.assertFalse(_is_import(match, (imported, used)))

  def test_context_filter_removes_only_import_only_blocks(self):
    separate = self._run(GG, "-A", "1", "os", "a.py")
    self.assertEqual(separate.returncode, 0, separate.stderr)
    self.assertNotIn("import os", separate.stdout)
    self.assertIn("os.getcwd", separate.stdout)

    merged = self._run(GG, "-C", "2", "os", "a.py")
    self.assertEqual(merged.returncode, 0, merged.stderr)
    self.assertIn("import os", merged.stdout)
    self.assertIn("os.getcwd", merged.stdout)

  def test_aggregate_modes_are_not_import_filtered(self):
    self.assertEqual(self._run(GG, "decimal").returncode, 1)

    paths = self._run(GG, "-l", "decimal")
    self.assertEqual(paths.returncode, 0, paths.stderr)
    self.assertEqual(paths.stdout, "dependencies.py\n")

    counts = self._run(GG, "-c", "decimal")
    self.assertEqual(counts.returncode, 0, counts.stderr)
    self.assertEqual(counts.stdout, "dependencies.py:1\n")

    hidden_count = self._run(GG, "-h", "-c", "decimal")
    self.assertEqual(hidden_count.returncode, 0, hidden_count.stderr)
    self.assertEqual(hidden_count.stdout, "1\n")

  def test_structured_renderer_honors_visible_fields(self):
    result = self._run(GG, "-h", "-n", "--column", "target", "a.py")
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertEqual(result.stdout, "3:5:def target():\n")

    null_result = self._run(GG, "-z", "target", "a.py")
    self.assertEqual(null_result.returncode, 0, null_result.stderr)
    self.assertEqual(null_result.stdout, "a.py\x003\x00def target():\n")

  def test_explicit_color_and_quiet_output(self):
    colored = self._run(GG, "--color=always", "target", "a.py")
    self.assertEqual(colored.returncode, 0, colored.stderr)
    self.assertIn("\x1b[", colored.stdout)

    uncolored = self._run_tty(GG, "--color=never", "target", "a.py")
    self.assertEqual(uncolored.returncode, 0, uncolored.stderr)
    self.assertNotIn("\x1b[", uncolored.stdout)

    quiet = self._run(GG, "-q", "target", "a.py")
    self.assertEqual(quiet.returncode, 0, quiet.stderr)
    self.assertEqual(quiet.stdout, "")
    self.assertEqual(self._run(GG, "-q", "missing_name").returncode, 1)

  def test_test_shortcuts_are_mutually_exclusive(self):
    for script in (GG, FINDDEF):
      with self.subTest(script=script.name):
        result = self._run(script, "-t", "-T", "target")
        self.assertEqual(result.returncode, 2)
        self.assertIn("not allowed with argument", result.stderr)

  def test_separated_git_grep_option_argument(self):
    result = self._run(GG, "-A", "1", "target", "a.py")
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
        commands = [shlex.split(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(commands), 2)
        for command in commands:
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
    for separator in ((), ("--",)):
      with self.subTest(separator=separator):
        self.assertEqual(
          self._run(GG, "TEST_ONLY", *separator, "tests").returncode, 1)
        included = self._run(
          GG, "+t", "--stdout", "TEST_ONLY", *separator, "tests", "/1")
        self.assertEqual(included.returncode, 0, included.stderr)
        self.assertTrue(
          included.stdout.startswith("tests/test_app.py:"), included.stdout)

  def test_finddef_accepts_paths_with_optional_separator(self):
    self.environment["GG_SEARCH_IN_DEFAULT"] = "nested/"
    for separator in ((), ("--",)):
      with self.subTest(separator=separator):
        result = self._run(FINDDEF, "target", *separator, "b.py")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("b.py:", result.stdout)

  def test_git_options_must_precede_the_search_term(self):
    result = self._run(GG, "target", "-i", "a.py")
    self.assertEqual(result.returncode, 2)
    self.assertIn("git grep options must precede PATTERN", result.stderr)

  def test_revision_and_path_arguments_are_left_for_git(self):
    result = self._run(GG, "target", "HEAD", "b.py")
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertIn("HEAD:b.py:", result.stdout)
    self.assertEqual(
      self._run(GG, "TEST_ONLY", "HEAD", "tests").returncode, 1)
    self.assertEqual(
      self._run(GG, "+t", "TEST_ONLY", "HEAD", "tests").returncode, 0)

  def test_help_explains_paths_and_effective_defaults(self):
    self.environment["GG_SEARCH_IN_DEFAULT"] = "nested/"
    self.environment["GG_SEARCH_EXT_DEFAULT"] = ".ts .tsx"
    result = self._run(GG, "--help")
    self.assertEqual(result.returncode, 0)
    help_text = " ".join(result.stdout.split())
    self.assertIn("[REV_OR_PATH ...] [/N]", help_text)
    self.assertIn("-- before paths is optional", help_text)
    self.assertIn("effective default: nested/", help_text)
    self.assertIn("effective default: .ts .tsx", help_text)
    self.assertIn("line numbers by default", help_text)
    self.assertIn(
      "add a search pattern; repeat to match alternatives", help_text)

  def test_short_help_is_only_special_without_other_arguments(self):
    help_result = self._run(GG, "-h")
    self.assertEqual(help_result.returncode, 0)
    self.assertIn("usage:", help_result.stdout)

    hidden = self._run(GG, "-h", "AlphaMarker")
    self.assertEqual(hidden.returncode, 0, hidden.stderr)
    self.assertEqual(hidden.stdout.strip(), "AlphaMarker = object()")

    missing_pattern = self._run(GG, "-n", "-h")
    self.assertEqual(missing_pattern.returncode, 2)
    self.assertIn("PATTERN is required", missing_pattern.stderr)

  def test_output_plan_classifies_git_output_modes(self):
    context_options = (
      ("-A", "2"),
      ("-B3",),
      ("-2",),
      ("--show-function",),
      ("-W",),
    )
    for arguments in context_options:
      with self.subTest(arguments=arguments):
        self.assertIs(
          make_output_plan(arguments).mode,
          GrepOutputMode.CONTEXT,
        )

    self.assertIs(
      make_output_plan(("-c", "-A", "2")).mode,
      GrepOutputMode.COUNTS,
    )
    self.assertIs(
      make_output_plan(("-c", "-l")).mode,
      GrepOutputMode.PATHS,
    )

  def test_without_context_options(self):
    arguments = (
      "-iA2",
      "-pWn",
      "-C", "3",
      "--before-context=4",
      "--after-context", "5",
      "-6",
      "--show-function",
      "--function-context",
      "--no-context",
      "--max-depth", "7",
      "-e", "-W",
      "--", "-B",
    )
    self.assertEqual(
      tuple(without_context_options(arguments)),
      ("-i", "-n", "--no-context", "--max-depth", "7", "-e", "-W", "--", "-B"),
    )

  def test_context_removal_preserves_attached_pager(self):
    self.assertEqual(
      tuple(without_context_options(("-Opager",))),
      ("-Opager",),
    )

  def test_context_removal_preserves_spaced_value_for_clustered_option(self):
    self.assertEqual(
      tuple(without_context_options(("-ie", "needle"))),
      ("-ie", "needle"),
    )

  def test_context_flags_do_not_hide_following_presentation_flags(self):
    plan, arguments = prepare_output(("-ph",))
    self.assertFalse(plan.show_filename)
    self.assertFalse(plan.show_line_number)
    self.assertEqual(arguments, ("-p",))

    plan, arguments = prepare_output(("-Wz",))
    self.assertTrue(plan.null_terminate_filenames)
    self.assertEqual(arguments, ("-W",))

  def test_context_removal_preserves_invalid_assignments(self):
    arguments = ("--show-function=value", "--function-context=value")
    self.assertEqual(tuple(without_context_options(arguments)), arguments)

  def test_output_plan_tracks_visible_fields(self):
    default = make_output_plan(())
    self.assertTrue(default.show_filename)
    self.assertTrue(default.show_line_number)
    self.assertFalse(default.show_column_number)

    hidden = make_output_plan(("-h",))
    self.assertFalse(hidden.show_filename)
    self.assertFalse(hidden.show_line_number)

    numbered = make_output_plan(("-h", "-n", "--column"))
    self.assertFalse(numbered.show_filename)
    self.assertTrue(numbered.show_line_number)
    self.assertTrue(numbered.show_column_number)

    count = make_output_plan(("-h", "-n", "--column", "-c"))
    self.assertFalse(count.show_filename)
    self.assertFalse(count.show_line_number)
    self.assertFalse(count.show_column_number)

    paths = make_output_plan(("-h", "-l"))
    self.assertTrue(paths.show_filename)
    self.assertFalse(paths.show_line_number)

  def test_output_plan_skips_pattern_option_values(self):
    plan = make_output_plan(("-e", "-h", "-f", "--column"))
    self.assertTrue(plan.show_filename)
    self.assertFalse(plan.show_column_number)

  def test_output_plan_honors_negated_options(self):
    plan = make_output_plan((
      "--count", "--no-count",
      "--context=2", "--no-context",
      "--heading", "--no-heading",
      "--color", "--no-color",
      "--quiet", "--no-quiet",
    ))
    self.assertIs(plan.mode, GrepOutputMode.MATCHES)
    self.assertFalse(plan.heading)
    self.assertEqual(plan.color, "never")
    self.assertFalse(plan.quiet)

  def test_output_preparation_removes_presentation_options(self):
    plan, arguments = prepare_output((
      "-hinA2",
      "--column",
      "--heading",
      "--color=always",
      "-z",
      "-c",
    ))
    self.assertIs(plan.mode, GrepOutputMode.COUNTS)
    self.assertEqual(arguments, ("-iA2", "-c"))

  def test_output_preparation_preserves_git_options_and_values(self):
    _, arguments = prepare_output((
      "-e", "--column",
      "-f", "-h",
      "--max-depth", "3",
      "--no-count",
    ))
    self.assertEqual(arguments, (
      "-e", "--column",
      "-f", "-h",
      "--max-depth", "3",
      "--no-count",
    ))

  def test_output_preparation_preserves_invalid_assignments(self):
    _, arguments = prepare_output((
      "--column=value", "--color=value", "--no-color=value",
    ))
    self.assertEqual(arguments, (
      "--column=value", "--color=value", "--no-color=value",
    ))

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

  def test_filename_only_results_can_be_selected(self):
    opened = self._run_tty(
      GG, "--tool", "echo", "-l", "AlphaMarker/1")
    self.assertEqual(opened.returncode, 0, opened.stderr)
    self.assertEqual(opened.stdout.strip(), "a.py")

    self.environment.pop("GG_TOOL_DEFAULT")
    displayed = self._run_tty(GG, "-l", "AlphaMarker/1")
    self.assertEqual(displayed.returncode, 0, displayed.stderr)
    self.assertIn("AlphaMarker = object()", displayed.stdout)
    self.assertNotIn("a.py:", displayed.stdout)

  def test_trailing_result_selection_after_paths(self):
    absolute_path = str(self.repository)
    attached = self._run(
      GG, "--stdout", "target/1", absolute_path)
    trailing = self._run(
      GG, "--stdout", "target", absolute_path, "/1")
    separated = self._run(
      GG, "--stdout", "target", "--", absolute_path, "/1")
    self.assertEqual(attached.returncode, 0, attached.stderr)
    self.assertEqual(trailing.returncode, 0, trailing.stderr)
    self.assertEqual(separated.returncode, 0, separated.stderr)
    self.assertEqual(attached.stdout, trailing.stdout)
    self.assertEqual(attached.stdout, separated.stdout)

  def test_duplicate_result_selection_is_rejected(self):
    attached = self._run(GG, "target/1", "a.py", "/2")
    self.assertEqual(attached.returncode, 2)
    self.assertIn("specified more than once", attached.stderr)

    strict = self._run(GG, "--show", "1", "target", "a.py", "/2")
    self.assertEqual(strict.returncode, 2)
    self.assertIn("either /N or --show N", strict.stderr)

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
