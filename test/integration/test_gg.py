from contextlib import redirect_stderr, redirect_stdout
import errno
import io
import json
import os
from pathlib import Path
import pty
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import gg
from lib.git_search import remove_color_codes
from test.support.gg_corpus import SEARCH_CORPUS_DIRECTORY, copy_corpus_tree, create_gg_repository
from test.support.git_grep_fixtures import GitGrepFixtureRunner


TOOLS_DIRECTORY = Path(__file__).resolve().parents[2]
GG = TOOLS_DIRECTORY / "gg.py"
DEF = TOOLS_DIRECTORY / "def"
OPEN_RESULT = TOOLS_DIRECTORY / "open_result.py"


class GGIntegrationTest(unittest.TestCase):
  @classmethod
  def setUpClass(cls):
    cls.temporary_directory = tempfile.TemporaryDirectory()
    cls.repository = Path(cls.temporary_directory.name)
    cls.base_environment = create_gg_repository(cls.repository, include_filesystem=True)
    for name in (
      "GG_SEARCH_IN", "GG_SEARCH_IN_DEFAULT",
      "GG_SEARCH_EXT", "GG_SEARCH_EXT_DEFAULT",
      "GG_TOOL", "GG_TOOL_DEFAULT",
    ):
      cls.base_environment.pop(name, None)
    cls.base_environment["GG_TOOL_DEFAULT"] = "tool-that-must-not-run"

  @classmethod
  def tearDownClass(cls):
    cls.temporary_directory.cleanup()

  def setUp(self):
    self.environment = self.base_environment.copy()

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
    for script in (GG, DEF):
      with self.subTest(script=script.name):
        result = self._run(script)
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

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

  def test_import_and_test_filters(self):
    result = self._run(GG, "os")
    self.assertEqual(result.returncode, 0)
    self.assertNotIn("import os", result.stdout)
    self.assertIn("os.getcwd", result.stdout)

    result = self._run(GG, "--imports", "os")
    self.assertIn("import os", result.stdout)

    result = self._run(GG, "import os")
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertIn("import os", result.stdout)

    self.assertEqual(self._run(GG, "TEST_ONLY").returncode, 1)
    self.assertEqual(self._run(GG, "+t", "TEST_ONLY").returncode, 0)
    self.assertEqual(
      self._run(GG, "--tests", "only", "TEST_ONLY").returncode, 0)

  def test_import_filter_only_checks_matched_text(self):
    result = self._run(GG, "PATH_MARKER")
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertIn("import directory/plain.py:", result.stdout)

  def test_explicit_color_and_quiet_output(self):
    colored = self._run(GG, "--color=always", "target", "valid_code.py")
    self.assertEqual(colored.returncode, 0, colored.stderr)
    self.assertIn("\x1b[", colored.stdout)

    uncolored = self._run_tty(GG, "--color=never", "target", "valid_code.py")
    self.assertEqual(uncolored.returncode, 0, uncolored.stderr)
    self.assertNotIn("\x1b[", uncolored.stdout)

    quiet = self._run(GG, "-q", "target", "valid_code.py")
    self.assertEqual(quiet.returncode, 0, quiet.stderr)
    self.assertEqual(quiet.stdout, "")
    self.assertEqual(self._run(GG, "-q", "missing_name").returncode, 1)

  def test_test_shortcuts_are_mutually_exclusive(self):
    for script in (GG, DEF):
      with self.subTest(script=script.name):
        result = self._run(script, "-t", "-T", "target")
        self.assertEqual(result.returncode, 2)
        self.assertIn("not allowed with argument", result.stderr)

  def test_separated_git_grep_option_argument(self):
    result = self._run(GG, "-A", "1", "target", "valid_code.py")
    self.assertEqual(result.returncode, 0)
    self.assertIn("target", result.stdout)

  def test_pattern_options_can_be_repeated_and_mixed(self):
    forms = (
      ("--pattern", "AlphaMarker", "--pattern", "BetaMarker"),
      ("--pattern", "AlphaMarker", "-e", "BetaMarker"),
      ("-e", "AlphaMarker", "--pattern", "BetaMarker"),
      ("-e", "AlphaMarker", "-e", "BetaMarker"),
      ("--pattern=AlphaMarker", "-eBetaMarker"),
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
    self.assertIn("AlphaMarker", result.stdout)
    self.assertIn("BetaMarker", result.stdout)

  def test_pattern_aliases_preserve_expression_order(self):
    expression = (
      "-e", "AlphaMarker", "--and", "(", "-e", "BetaMarker", ")",
    )
    result = self._run(
      GG, "+print", "-e", "AlphaMarker", "--and", "(",
      "--pattern", "BetaMarker", ")",
    )
    self.assertEqual(result.returncode, 0, result.stderr)
    for line in result.stdout.splitlines():
      command = shlex.split(line)
      start = command.index("-e")
      self.assertEqual(tuple(command[start:start + len(expression)]), expression)

  def test_pattern_option_at_end_of_short_option_cluster(self):
    result = self._run(GG, "+print", "-iPe", "AlphaMarker")
    self.assertEqual(result.returncode, 0, result.stderr)
    for line in result.stdout.splitlines():
      command = shlex.split(line)
      self.assertIn("-iP", command)
      pattern_index = command.index("-e")
      self.assertEqual(command[pattern_index + 1], "AlphaMarker")
      self.assertNotIn("gg-pattern-internal", line)

  def test_pattern_aliases_after_separator_are_not_rewritten(self):
    result = self._run(
      GG, "+print", "AlphaMarker", "--", "--pattern=literal-path",
    )
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertIn("--pattern=literal-path", result.stdout)
    self.assertNotIn("gg-pattern-internal", result.stdout)

  def test_repeated_patterns_support_trailing_result_selection(self):
    result = self._run(
      GG, "--stdout",
      "--pattern", "AlphaMarker", "--pattern", "BetaMarker",
      "valid_code.py", "/2",
    )
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertTrue(result.stdout.startswith("valid_code.py:"), result.stdout)

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

  def test_definition_search_accepts_paths_with_optional_separator(self):
    self.environment["GG_SEARCH_IN_DEFAULT"] = "frontend/"
    for separator in ((), ("--",)):
      with self.subTest(separator=separator):
        result = self._run(DEF, "scoped", *separator, "nested/scoped.py")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("nested/scoped.py:", result.stdout)

  def test_git_options_must_precede_the_search_term(self):
    result = self._run(GG, "scoped", "-i", "nested/scoped.py")
    self.assertEqual(result.returncode, 2)
    self.assertIn("git grep options must precede PATTERN", result.stderr)

  def test_revision_and_path_arguments_are_left_for_git(self):
    result = self._run(GG, "scoped", "HEAD", "nested/scoped.py")
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertIn("HEAD:nested/scoped.py:", result.stdout)
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

  def test_explicit_path_options_are_not_silently_ignored(self):
    for option in (("--root", "nested/"), ("--ext", "py")):
      with self.subTest(option=option[0]):
        result = self._run(GG, *option, "scoped", "--", "nested/scoped.py")
        self.assertEqual(result.returncode, 2)
        self.assertIn("cannot be combined", result.stderr)

  def test_untracked_files_are_explicit(self):
    self.assertEqual(self._run(DEF, "Untracked").returncode, 1)
    result = self._run(DEF, "--untracked", "Untracked")
    self.assertEqual(result.returncode, 0)
    self.assertIn("untracked.py", result.stdout)
    self.assertEqual(
      self._run(DEF, "--untracked", "Ignored").returncode, 1)

  def test_selection_takes_priority_over_other_incompatible_output(self):
    for arguments in (("-c",), ("-q",), ("-o",), ("-Oviewer",)):
      with self.subTest(arguments=arguments):
        result = self._run(DEF, *arguments, "--stdout", "target/1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("valid_code.py:"), result.stdout)

  def test_json_takes_priority_over_aggregate_and_context_output(self):
    result = self._run(GG, "--json", "-lC", "2", "target")
    self.assertEqual(result.returncode, 0, result.stderr)
    records = [json.loads(line) for line in result.stdout.splitlines()]
    self.assertGreaterEqual(len(records), 2)
    self.assertIsInstance(records[0]["line"], int)
    self.assertIn("target", records[0]["text"])

  def test_filename_only_results_can_be_selected(self):
    opened = self._run_tty(
      GG, "--tool", "echo", "-l", "AlphaMarker/1")
    self.assertEqual(opened.returncode, 0, opened.stderr)
    self.assertEqual(opened.stdout.strip(), "valid_code.py")

    self.environment.pop("GG_TOOL_DEFAULT")
    displayed = self._run_tty(GG, "-l", "AlphaMarker/1")
    self.assertEqual(displayed.returncode, 0, displayed.stderr)
    self.assertIn("AlphaMarker = object()", displayed.stdout)
    self.assertNotIn("valid_code.py:", displayed.stdout)

  def test_trailing_result_selection_after_paths(self):
    absolute_path = str(self.repository)
    attached = self._run(
      GG, "--stdout", "scoped/1", absolute_path)
    trailing = self._run(
      GG, "--stdout", "scoped", absolute_path, "/1")
    separated = self._run(
      GG, "--stdout", "scoped", "--", absolute_path, "/1")
    self.assertEqual(attached.returncode, 0, attached.stderr)
    self.assertEqual(trailing.returncode, 0, trailing.stderr)
    self.assertEqual(separated.returncode, 0, separated.stderr)
    self.assertEqual(attached.stdout, trailing.stdout)
    self.assertEqual(attached.stdout, separated.stdout)

  def test_duplicate_result_selection_is_rejected(self):
    attached = self._run(GG, "scoped/1", "nested/scoped.py", "/2")
    self.assertEqual(attached.returncode, 2)
    self.assertIn("specified more than once", attached.stderr)

    strict = self._run(GG, "--show", "1", "scoped", "nested/scoped.py", "/2")
    self.assertEqual(strict.returncode, 2)
    self.assertIn("either /N or --show N", strict.stderr)

  def test_non_tty_selection_does_not_launch_editor(self):
    result = self._run(DEF, "target/1")
    self.assertEqual(result.returncode, 0)
    self.assertTrue(result.stdout.startswith("valid_code.py:"), result.stdout)

  def test_open_result_uses_shared_selection_and_display(self):
    environment = self.environment.copy()
    environment.pop("GG_TOOL_DEFAULT")
    result = subprocess.run(
      ("python", str(OPEN_RESULT), "999"),
      cwd=self.repository,
      env=environment,
      input="valid_code.py:3:def target():\nvalid_code.py:16:def target():\n",
      capture_output=True,
      text=True,
    )
    self.assertEqual(result.returncode, 0)
    self.assertTrue(result.stdout.startswith("valid_code.py:16:0\n"), result.stdout)


class GGFixtureIntegrationTest(unittest.TestCase):
  @classmethod
  def setUpClass(cls):
    cls.temporary_directory = tempfile.TemporaryDirectory()
    cls.repository = Path(cls.temporary_directory.name)
    copy_corpus_tree(SEARCH_CORPUS_DIRECTORY, cls.repository)
    cls.environment = os.environ.copy()
    for name in (
      "GG_SEARCH_IN", "GG_SEARCH_IN_DEFAULT",
      "GG_SEARCH_EXT", "GG_SEARCH_EXT_DEFAULT",
      "GG_TOOL", "GG_TOOL_DEFAULT",
      "GG_GIT_VERSION",
    ):
      cls.environment.pop(name, None)

  @classmethod
  def tearDownClass(cls):
    cls.temporary_directory.cleanup()

  def _run(self, *arguments):
    fixture_runner = GitGrepFixtureRunner()
    output_bytes = io.BytesIO()
    output = io.TextIOWrapper(output_bytes, encoding="utf-8")
    errors = io.StringIO()
    previous_directory = Path.cwd()

    try:
      os.chdir(self.repository)
      with (
        patch.dict(os.environ, self.environment, clear=True),
        patch("lib.git_search.subprocess.run", fixture_runner),
        redirect_stdout(output),
        redirect_stderr(errors),
      ):
        status = gg.main(arguments)
        output.flush()
    finally:
      os.chdir(previous_directory)

    return subprocess.CompletedProcess(
      arguments, status, output_bytes.getvalue().decode(), errors.getvalue())

  def test_exit_codes_distinguish_matches_and_errors(self):
    self.assertEqual(self._run("gg", "target").returncode, 0)
    self.assertEqual(self._run("gg", "missing_name").returncode, 1)
    self.assertGreaterEqual(self._run("gg", "--bad-git-option", "target").returncode, 2)

  def test_default_word_search(self):
    result = self._run("gg", "target")
    self.assertEqual(result.returncode, 0)
    self.assertNotIn("target_extra", result.stdout)

  def test_import_filtering(self):
    result = self._run("gg", "os")
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertNotIn("import os", result.stdout)
    self.assertIn("os.getcwd", result.stdout)

    result = self._run("gg", "--imports", "os")
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertIn("import os", result.stdout)

    result = self._run("gg", "import os")
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertIn("import os", result.stdout)

  def test_completed_multiline_python_import_is_filtered(self):
    result = self._run("gg", "ImportedMarker")
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertNotIn("  ImportedMarker)", result.stdout)
    self.assertIn("ImportedMarker()", result.stdout)

  def test_incomplete_multiline_python_import_is_kept(self):
    result = self._run("gg", "UncertainMarker")
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertIn("UncertainMarker", result.stdout)

  def test_context_filter_removes_only_import_only_blocks(self):
    separate = self._run("gg", "-A", "1", "os", "valid_code.py")
    self.assertEqual(separate.returncode, 0, separate.stderr)
    self.assertNotIn("import os", separate.stdout)
    self.assertIn("os.getcwd", separate.stdout)

    merged = self._run("gg", "-C", "2", "os", "valid_code.py")
    self.assertEqual(merged.returncode, 0, merged.stderr)
    self.assertIn("import os", merged.stdout)
    self.assertIn("os.getcwd", merged.stdout)

  def test_aggregate_modes_are_not_import_filtered(self):
    self.assertEqual(self._run("gg", "decimal").returncode, 1)

    paths = self._run("gg", "-l", "decimal")
    self.assertEqual(paths.returncode, 0, paths.stderr)
    self.assertEqual(paths.stdout, "valid_code.py\n")

    counts = self._run("gg", "-c", "decimal")
    self.assertEqual(counts.returncode, 0, counts.stderr)
    self.assertEqual(counts.stdout, "valid_code.py:1\n")

    hidden_count = self._run("gg", "-h", "-c", "decimal")
    self.assertEqual(hidden_count.returncode, 0, hidden_count.stderr)
    self.assertEqual(hidden_count.stdout, "1\n")

  def test_structured_renderer_honors_visible_fields(self):
    result = self._run("gg", "-h", "-n", "--column", "target_extra", "valid_code.py")
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertEqual(result.stdout, "6:5:def target_extra():\n")

    null_result = self._run("gg", "-z", "target_extra", "valid_code.py")
    self.assertEqual(null_result.returncode, 0, null_result.stderr)
    self.assertEqual(null_result.stdout, "valid_code.py\x006\x00def target_extra():\n")

  def test_quoted_search_crop_matches_output_includes_quotes(self):
    result = self._run("gq", "-o", "--pattern", "route/123", "valid_code.py")
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertTrue(result.stdout.endswith(':"route/123"\n'), result.stdout)
    self.assertNotIn("route =", result.stdout)

  def test_definition_crop_matches_output_includes_definition_syntax(self):
    result = self._run("def", "-o", "target_extra", "valid_code.py")
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertTrue(result.stdout.endswith(":def target_extra\n"), result.stdout)

  def test_crop_matches_output_without_a_semantic_filter(self):
    result = self._run("gg", "--imports", "-o", "decimal", "valid_code.py")
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertTrue(result.stdout.endswith(":decimal\n"), result.stdout)

  def test_json_output(self):
    result = self._run("gg", "--json", "target")
    self.assertEqual(result.returncode, 0, result.stderr)
    records = [json.loads(line) for line in result.stdout.splitlines()]
    self.assertGreaterEqual(len(records), 2)
    self.assertEqual(set(records[0]), {"path", "line", "column", "text"})

  def test_crop_matches_json_output_contains_projected_occurrences(self):
    result = self._run("def", "--json", "-o", "target_extra", "valid_code.py")
    self.assertEqual(result.returncode, 0, result.stderr)
    records = [json.loads(line) for line in result.stdout.splitlines()]
    self.assertEqual(
      [(record["column"], record["text"]) for record in records],
      [(1, "def target_extra")],
    )

  def test_crop_matches_projection_precedes_result_selection(self):
    result = self._run("def", "-o", "--stdout", "target/1", "valid_code.py")
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertTrue(result.stdout.startswith("valid_code.py:3:1\n"), result.stdout)

  def test_selection_takes_priority_over_context_output(self):
    result = self._run("def", "-C", "3", "--stdout", "target/1")
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertTrue(result.stdout.startswith("valid_code.py:"), result.stdout)

  def test_human_and_strict_selection(self):
    first = self._run("def", "target/0", "--stdout")
    self.assertEqual(first.returncode, 0, first.stderr)
    self.assertTrue(first.stdout.startswith("valid_code.py:3:"), first.stdout)

    last = self._run("def", "target/999", "--stdout")
    self.assertEqual(last.returncode, 0, last.stderr)
    self.assertTrue(last.stdout.startswith("valid_code.py:16:"), last.stdout)

    strict = self._run("def", "--pattern", "target", "--show", "999")
    self.assertEqual(strict.returncode, 2)
    self.assertIn("outside the result range", strict.stderr)

  def test_explicit_pattern_disables_slash_selection(self):
    result = self._run("gg", "--pattern", "route/123")
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertIn('route = "route/123"', result.stdout)


if __name__ == "__main__":
  unittest.main()
