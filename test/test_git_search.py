import errno
import json
import os
from pathlib import Path
import pty
import shlex
import subprocess
import tempfile
import unittest

from lib.git_search import ContextBlock, remove_color_codes, SearchResult, GrepOutputMode, ResultAction
from lib.git_search_args import _split_leading_git_options, ArgumentIterator, make_output_plan, prepare_occurrence_options, skip_only_matching_options
from gg import ImportScanner, PythonImportScanner, _TYPESCRIPT_MULTILINE_IMPORT, _filter_occurrence_contexts, _filter_occurrence_matches, _has_definition_occurrence, _has_quoted_occurrence, _has_non_import, _update_typescript_import_state


TOOLS_DIRECTORY = Path(__file__).resolve().parents[1]
GG = TOOLS_DIRECTORY / "gg.py"
DEF = TOOLS_DIRECTORY / "def"
OPEN_RESULT = TOOLS_DIRECTORY / "open_result.py"


def _result_action(*, index=None, json=False):
  return ResultAction(
    index=index,
    clamp_index=False,
    json=json,
    open_result=False,
    stdout=True,
    tool=None,
  )


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
    self._write("multiline_import.py", """\
from package import (
  ImportedMarker)

ImportedMarker()
""")
    self._write("incomplete_import.py", """\
from package import (
  UncertainMarker,
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

  def _has_non_import(self, match, occurrences, patterns):
    with ImportScanner() as import_scanner, PythonImportScanner() as python_import_scanner:
      return _has_non_import(
        match, occurrences, patterns, import_scanner, python_import_scanner)

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

  def test_completed_multiline_python_import_is_filtered(self):
    result = self._run(GG, "ImportedMarker")
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertNotIn("  ImportedMarker)", result.stdout)
    self.assertIn("ImportedMarker()", result.stdout)

  def test_incomplete_multiline_python_import_is_kept(self):
    result = self._run(GG, "UncertainMarker")
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertIn("UncertainMarker", result.stdout)

  def test_python_import_scanner_reuses_incremental_parser(self):
    path = str(self.repository / "multiline_import.py")
    with PythonImportScanner() as scanner:
      self.assertIsNone(scanner.classify(path, 1))
      self.assertTrue(scanner.classify(path, 2))
      self.assertFalse(scanner.classify(path, 4))

  def test_import_scanner_returns_unknown_if_requested_line_was_not_read(self):
    path = self.repository / "frontend/incomplete.ts"
    self._write(path.relative_to(self.repository), "import {\n  MissingSymbol,\n")
    with ImportScanner() as scanner:
      self.assertTrue(scanner.classify(str(path), 2, _update_typescript_import_state))
      self.assertIsNone(scanner.classify(str(path), 20, _update_typescript_import_state))

  def test_import_scanner_restarts_at_lookback_window_after_large_gap(self):
    path = self.repository / "frontend/long_import.ts"
    self._write(path.relative_to(self.repository), """\
first line
second line
import {
  FirstSymbol,
  SecondSymbol,
  ThirdSymbol,
""")
    with ImportScanner(lookback=1, max_gap=2) as scanner:
      self.assertFalse(scanner.classify(str(path), 6, _update_typescript_import_state))

  def test_import_scanner_ignores_complete_typescript_imports(self):
    path = self.repository / "frontend/single_import.ts"
    self._write(path.relative_to(self.repository), """\
import {ExistingBinding} from "dependency"
IndependentBinding
""")
    with ImportScanner() as scanner:
      self.assertFalse(scanner.classify(str(path), 2, _update_typescript_import_state))

  def test_typescript_multiline_import_headers(self):
    headers = (
      b"import {\n",
      b"import type {\n",
      b"import DefaultBinding, {\n",
    )
    for header in headers:
      with self.subTest(header=header):
        self.assertIsNotNone(_TYPESCRIPT_MULTILINE_IMPORT.match(header))

  def test_coffeescript_comma_list_match_is_not_scanned(self):
    path = self.repository / "legacy/module.coffee"
    self._write(path.relative_to(self.repository), """\
{ExistingBinding} = require "dependency"
IndependentBinding
""")
    match = SearchResult(str(path), 2, 1, b"IndependentBinding")
    self.assertTrue(self._has_non_import(match, (), ("IndependentBinding",)))

  def test_go_import_filter(self):
    path = self.repository / "backend/module.go"
    self._write(path.relative_to(self.repository), """\
package backend

import SingleAlias "module/one"
import (
  // retained comment
  GroupAlias "module/two"
  _ "module/driver"
  . "module/helpers"
)

var Result = GroupAlias
""")

    def has_non_import(line, text, name):
      encoded_name = name.encode()
      match = SearchResult(str(path), line, 1, text)
      occurrence = SearchResult(str(path), line, text.index(encoded_name) + 1, encoded_name)
      return _has_non_import(
        match, (occurrence,), (name,), import_scanner, python_import_scanner)

    with ImportScanner() as import_scanner, PythonImportScanner() as python_import_scanner:
      self.assertFalse(has_non_import(3, b'import SingleAlias "module/one"', "SingleAlias"))
      self.assertFalse(has_non_import(6, b'  GroupAlias "module/two"', "GroupAlias"))
      self.assertFalse(has_non_import(7, b'  _ "module/driver"', "driver"))
      self.assertFalse(has_non_import(8, b'  . "module/helpers"', "helpers"))
      self.assertTrue(has_non_import(11, b"var Result = GroupAlias", "GroupAlias"))

  def test_import_filter_ignores_color_codes(self):
    match = SearchResult(
      "module.py", 1, 1, b"\x1b[1;31mimport\x1b[m dependency")
    occurrence = SearchResult("module.py", 1, 8, b"dependency")
    self.assertFalse(self._has_non_import(match, (occurrence,), ("dependency",)))

  def test_occurrence_import_filter_keeps_mixed_lines(self):
    match = SearchResult(
      "module.py", 1, 1, b"import package; package.run()")
    imported = SearchResult("module.py", 1, 8, b"package")
    used = SearchResult("module.py", 1, 17, b"package")
    self.assertFalse(self._has_non_import(match, (imported,), ("package",)))
    self.assertTrue(self._has_non_import(match, (imported, used), ("package",)))

  def test_import_filter_checks_patterns_for_the_matched_language(self):
    python_match = SearchResult("module.py", 1, 1, b"import dependency")
    python_occurrence = SearchResult("module.py", 1, 8, b"dependency")
    coffee_match = SearchResult("module.coffee", 1, 1, b"{dependency} = require 'package'")
    coffee_occurrence = SearchResult("module.coffee", 1, 2, b"dependency")

    self.assertTrue(self._has_non_import(
      python_match, (python_occurrence,), ("import dependency",)))
    self.assertFalse(self._has_non_import(
      python_match, (python_occurrence,), ("{dependency} = require",)))
    self.assertTrue(self._has_non_import(
      coffee_match, (coffee_occurrence,), ("{dependency} = require",)))
    self.assertFalse(self._has_non_import(
      coffee_match, (coffee_occurrence,), ("import dependency",)))

  def test_quoted_filter_uses_byte_columns(self):
    match = SearchResult("module.py", 1, 1, b'\xc3\xa9 "marker"')
    occurrence = SearchResult("module.py", 1, 5, b"marker")
    self.assertTrue(_has_quoted_occurrence(match, (occurrence,)))

  def test_quoted_search_ignores_only_matching_output_mode(self):
    script = self.repository / "gq"
    script.symlink_to(GG)
    python_path = self.environment.get("PYTHONPATH")
    self.environment["PYTHONPATH"] = os.pathsep.join(
      path for path in (str(TOOLS_DIRECTORY), python_path) if path
    )

    result = self._run(script, "-o", "--pattern", "route/123", "a.py")

    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertIn('route = "route/123"', result.stdout)
    self.assertNotIn("Traceback", result.stderr)

  def test_definition_filter_checks_the_occurrence_position(self):
    match = SearchResult("module.py", 1, 1, b"def selected(argument): return selected")
    name = SearchResult("module.py", 1, 5, b"selected")
    argument = SearchResult("module.py", 1, 14, b"argument")
    body = SearchResult("module.py", 1, 32, b"selected")
    self.assertTrue(_has_definition_occurrence(match, (name,)))
    self.assertFalse(_has_definition_occurrence(match, (argument, body)))

  def test_definition_filter_handles_classes_async_functions_and_partial_matches(self):
    cases = (
      (b"class ServiceFactory(Base):", 14, b"Factory"),
      (b"  async def load_resource():", 13, b"load"),
    )
    for text, column, occurrence_text in cases:
      with self.subTest(text=text):
        match = SearchResult("module.py", 1, 1, text)
        occurrence = SearchResult("module.py", 1, column, occurrence_text)
        self.assertTrue(_has_definition_occurrence(match, (occurrence,)))

  def test_definition_filter_handles_typescript_declarations(self):
    cases = (
      ("model.ts", b"export abstract class DataStore<T> {}", b"DataStore"),
      ("model.ts", b"export interface ResultShape {}", b"ResultShape"),
      ("model.ts", b"export type ResultValue = string", b"ResultValue"),
      ("model.ts", b"const enum StateCode {}", b"StateCode"),
      ("model.ts", b"export namespace Utilities {}", b"Utilities"),
      ("model.ts", b"declare module Internal {}", b"Internal"),
      ("model.ts", b"export default async function* loadItems() {}", b"loadItems"),
    )
    for path, text, name in cases:
      with self.subTest(text=text):
        occurrence = SearchResult(path, 1, text.index(name) + 1, name)
        match = SearchResult(path, 1, occurrence.column, text)
        self.assertTrue(_has_definition_occurrence(match, (occurrence,)))

  def test_definition_filter_handles_simple_typescript_variables(self):
    cases = (
      ("state.ts", b"export const cacheEntry: string = value", b"cacheEntry"),
      ("state.ts", b"declare let activeFlag: boolean", b"activeFlag"),
      ("view.tsx", b"var fallbackValue = null", b"fallbackValue"),
    )
    for path, text, name in cases:
      with self.subTest(text=text):
        occurrence = SearchResult(path, 1, text.index(name) + 1, name)
        match = SearchResult(path, 1, occurrence.column, text)
        self.assertTrue(_has_definition_occurrence(match, (occurrence,)))

  def test_definition_filter_handles_simple_go_declarations(self):
    cases = (
      (b"func Render() {}", b"Render"),
      (b"func (node *Tree[T]) Render() {}", b"Render"),
      (b"type Tree[T any] struct{}", b"Tree"),
      (b"type TreeAlias = Tree[int]", b"TreeAlias"),
      (b"var root *Tree[int]", b"root"),
      (b"const defaultDepth = 4", b"defaultDepth"),
      (b"child := makeNode()", b"child"),
    )
    for text, name in cases:
      with self.subTest(text=text):
        occurrence = SearchResult("tree.go", 1, text.index(name) + 1, name)
        match = SearchResult("tree.go", 1, occurrence.column, text)
        self.assertTrue(_has_definition_occurrence(match, (occurrence,)))

  def test_definition_filter_rejects_go_usages(self):
    cases = (
      (b"func Render() { Render() }", b"Render"),
      (b"var root = makeNode()", b"makeNode"),
      (b"child := makeNode()", b"makeNode"),
    )
    for text, name in cases:
      with self.subTest(text=text):
        column = text.rindex(name) + 1
        occurrence = SearchResult("tree.go", 1, column, name)
        match = SearchResult("tree.go", 1, occurrence.column, text)
        self.assertFalse(_has_definition_occurrence(match, (occurrence,)))

  def test_definition_filter_rejects_typescript_usages_and_destructuring(self):
    cases = (
      (b"const container = transform(sourceValue)", b"sourceValue"),
      (b"const created = new Service()", b"Service"),
      (b"const {selected} = source", b"selected"),
      (b"const enum StateCode {}", b"enum"),
    )
    for text, name in cases:
      with self.subTest(text=text):
        occurrence = SearchResult("module.ts", 1, text.index(name) + 1, name)
        match = SearchResult("module.ts", 1, occurrence.column, text)
        self.assertFalse(_has_definition_occurrence(match, (occurrence,)))

  def test_definition_filter_is_language_aware(self):
    match = SearchResult("module.txt", 1, 1, b"def selected():")
    occurrence = SearchResult("module.txt", 1, 5, b"selected")
    self.assertFalse(_has_definition_occurrence(match, (occurrence,)))

  def test_occurrence_filter_finds_definitions_in_match_results(self):
    results = (
      SearchResult("module.py", 1, 5, b"def selected():"),
      SearchResult("module.py", 2, 3, b"selected()"),
    )
    occurrences = (
      SearchResult("module.py", 1, 5, b"selected"),
      SearchResult("module.py", 2, 1, b"selected"),
    )
    self.assertEqual(
      list(_filter_occurrence_matches(results, occurrences, _has_definition_occurrence)),
      [results[0]],
    )

  def test_occurrence_context_filter_consumes_every_match_line(self):
    blocks = (
      ContextBlock("module.py", (
        SearchResult("module.py", 1, 5, b"def first():"),
        SearchResult("module.py", 2, 3, b"first()"),
      )),
      ContextBlock("module.py", (
        SearchResult("module.py", 8, 15, b"          def second():"),
      )),
    )
    occurrences = (
      SearchResult("module.py", 1, 5, b"first"),
      SearchResult("module.py", 2, 1, b"first"),
      SearchResult("module.py", 8, 15, b"second"),
    )
    checked_lines = []
    def predicate(match, line_occurrences):
      checked_lines.append(match.line)
      return _has_definition_occurrence(match, line_occurrences)

    self.assertEqual(list(_filter_occurrence_contexts(blocks, occurrences, predicate)), list(blocks))
    self.assertEqual(checked_lines, [1, 2, 8])

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
    for script in (GG, DEF):
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
    self.assertIn("a.py:", result.stdout)
    self.assertIn("b.py:", result.stdout)

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

  def test_definition_search_accepts_paths_with_optional_separator(self):
    self.environment["GG_SEARCH_IN_DEFAULT"] = "nested/"
    for separator in ((), ("--",)):
      with self.subTest(separator=separator):
        result = self._run(DEF, "target", *separator, "b.py")
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
          make_output_plan(arguments)[0].mode,
          GrepOutputMode.CONTEXT,
        )

    self.assertIs(
      make_output_plan(("-c", "-A", "2"))[0].mode,
      GrepOutputMode.COUNTS,
    )
    self.assertIs(
      make_output_plan(("-c", "-l"))[0].mode,
      GrepOutputMode.PATHS,
    )

  def test_occurrence_options_remove_context_options(self):
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
    )
    self.assertEqual(
      tuple(prepare_occurrence_options(arguments)),
      ("-i", "-n", "--no-context", "--max-depth", "7", "-e", "-W", "-o"),
    )

  def test_occurrence_options_have_one_only_matching_option(self):
    cases = (
      (("-o",), ("-o",)),
      (("--only-matching",), ("-o",)),
      (("--only",), ("-o",)),
      (("-ioo",), ("-i", "-o")),
      (("-A2", "-o", "-n"), ("-n", "-o")),
    )
    for arguments, expected in cases:
      with self.subTest(arguments=arguments):
        self.assertEqual(tuple(prepare_occurrence_options(arguments)), expected)

  def test_only_matching_options_can_be_removed_from_result_search(self):
    cases = (
      (("-io",), ("-i",)),
      (("-oo",), ()),
      (("--only-matching", "-n"), ("-n",)),
      (("--only",), ()),
    )
    for arguments, expected in cases:
      with self.subTest(arguments=arguments):
        self.assertEqual(tuple(skip_only_matching_options(arguments)), expected)

  def test_argument_iterator_is_a_pass_through_iterator(self):
    cases = (
      (),
      ("",),
      ("plain", ""),
      ("-in", "-A2"),
      ("-e", ""),
      ("--max-d", "4", "--threads=2"),
      ("-", "--123", "-i-"),
      ("-e", "--", "-A", "2"),
      ("--", "-A", "2", ""),
    )
    for arguments in cases:
      with self.subTest(arguments=arguments):
        options = ArgumentIterator(arguments)
        self.assertIs(iter(options), options)
        self.assertEqual(tuple(options), arguments)

  def test_leading_git_options_use_shared_option_parsing(self):
    class Parser:
      def error(self, message):
        raise AssertionError(message)

    arguments = [
      "-iA", "2", "-efirst_expression", "-fpatterns.txt",
      "fallback_expression", "source",
    ]
    options, operands, patterns, has_pattern_source, all_patterns_known = _split_leading_git_options(arguments, Parser())
    self.assertEqual(options, arguments[:4])
    self.assertEqual(operands, arguments[4:])
    self.assertEqual(patterns, ("first_expression",))
    self.assertTrue(has_pattern_source)
    self.assertFalse(all_patterns_known)

  def test_incompatible_output_options_are_removed(self):
    arguments = (
      "-icqol",
      "-A2",
      "-C", "3",
      "-7",
      "--before-context=4",
      "--function-con",
      "--only-matching",
      "--open-files-in-pager=viewer",
      "--files-with-matches",
      "--max-depth", "5",
    )
    plan, filtered = make_output_plan(arguments, _result_action(json=True))
    self.assertIs(plan.mode, GrepOutputMode.MATCHES)
    self.assertEqual(filtered, ("-i", "--max-depth", "5"))

  def test_incompatible_output_filter_can_preserve_path_modes(self):
    arguments = ("-ilL", "--name-only", "--files-without-match", "-c")
    plan, filtered = make_output_plan(arguments, _result_action(index=1))
    self.assertIs(plan.mode, GrepOutputMode.PATHS)
    self.assertEqual(filtered, ("-ilL", "--name-only", "--files-without-match"))

  def test_incompatible_output_filter_preserves_option_values_and_operands(self):
    arguments = ("-e", "-C", "-C", "2", "--", "-A", "3")
    plan, filtered = make_output_plan(arguments, _result_action(json=True))
    self.assertIs(plan.mode, GrepOutputMode.MATCHES)
    self.assertEqual(filtered, ("-e", "-C", "--", "-A", "3"))

  def test_occurrence_options_preserve_attached_pager(self):
    self.assertEqual(
      tuple(prepare_occurrence_options(("-Opager",))),
      ("-Opager", "-o"),
    )

  def test_occurrence_options_preserve_spaced_value_for_clustered_option(self):
    self.assertEqual(
      tuple(prepare_occurrence_options(("-ie", "needle"))),
      ("-ie", "needle", "-o"),
    )

  def test_occurrence_options_preserve_spaced_value_for_abbreviated_option(self):
    arguments = ("--max-d", "4", "-A", "2")
    self.assertEqual(tuple(prepare_occurrence_options(arguments)), ("--max-d", "4", "-o"))

  def test_occurrence_options_preserve_empty_spaced_value(self):
    self.assertEqual(tuple(prepare_occurrence_options(("-e", ""))), ("-e", "", "-o"))

  def test_occurrence_options_do_not_repeat_equals_value(self):
    for arguments in (("--threads=3",), ("--max-d=4",)):
      with self.subTest(arguments=arguments):
        self.assertEqual(tuple(prepare_occurrence_options(arguments)), (*arguments, "-o"))

  def test_occurrence_options_preserve_incomplete_and_invalid_options(self):
    for arguments in (("-ie",), ("-",), ("--123",)):
      with self.subTest(arguments=arguments):
        self.assertEqual(tuple(prepare_occurrence_options(arguments)), (*arguments, "-o"))

  def test_context_flags_do_not_hide_following_presentation_flags(self):
    plan, arguments = make_output_plan(("-ph",))
    self.assertFalse(plan.show_filename)
    self.assertFalse(plan.show_line_number)
    self.assertEqual(arguments, ("-p",))

    plan, arguments = make_output_plan(("-Wz",))
    self.assertTrue(plan.null_terminate_filenames)
    self.assertEqual(arguments, ("-W",))

  def test_occurrence_options_preserve_invalid_assignments(self):
    arguments = ("--show-function=value", "--function-context=value")
    self.assertEqual(tuple(prepare_occurrence_options(arguments)), (*arguments, "-o"))

  def test_occurrence_options_recognize_abbreviated_long_options(self):
    arguments = ("--bef=2", "--cont", "3", "--show-f", "--function-con", "-i")
    self.assertEqual(tuple(prepare_occurrence_options(arguments)), ("-i", "-o"))

  def test_output_plan_recognizes_abbreviated_context_options(self):
    for arguments in (("--bef=2",), ("--show-f",), ("--function-con",)):
      with self.subTest(arguments=arguments):
        self.assertIs(make_output_plan(arguments)[0].mode, GrepOutputMode.CONTEXT)

  def test_output_plan_tracks_visible_fields(self):
    default, _ = make_output_plan(())
    self.assertTrue(default.show_filename)
    self.assertTrue(default.show_line_number)
    self.assertFalse(default.show_column_number)

    hidden, _ = make_output_plan(("-h",))
    self.assertFalse(hidden.show_filename)
    self.assertFalse(hidden.show_line_number)

    numbered, _ = make_output_plan(("-h", "-n", "--column"))
    self.assertFalse(numbered.show_filename)
    self.assertTrue(numbered.show_line_number)
    self.assertTrue(numbered.show_column_number)

    count, _ = make_output_plan(("-h", "-n", "--column", "-c"))
    self.assertFalse(count.show_filename)
    self.assertFalse(count.show_line_number)
    self.assertFalse(count.show_column_number)

    paths, _ = make_output_plan(("-h", "-l"))
    self.assertTrue(paths.show_filename)
    self.assertFalse(paths.show_line_number)

  def test_output_plan_skips_pattern_option_values(self):
    plan, _ = make_output_plan(("-e", "-h", "-f", "--column"))
    self.assertTrue(plan.show_filename)
    self.assertFalse(plan.show_column_number)

  def test_output_plan_honors_negated_options(self):
    plan, _ = make_output_plan((
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
    plan, arguments = make_output_plan((
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
    _, arguments = make_output_plan((
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
    _, arguments = make_output_plan((
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
    self.assertEqual(self._run(DEF, "Untracked").returncode, 1)
    result = self._run(DEF, "--untracked", "Untracked")
    self.assertEqual(result.returncode, 0)
    self.assertIn("untracked.py", result.stdout)
    self.assertEqual(
      self._run(DEF, "--untracked", "Ignored").returncode, 1)

  def test_json_output(self):
    result = self._run(GG, "--json", "target")
    self.assertEqual(result.returncode, 0)
    records = [json.loads(line) for line in result.stdout.splitlines()]
    self.assertGreaterEqual(len(records), 2)
    self.assertEqual(
      set(records[0]), {"path", "line", "column", "text"})

  def test_selection_takes_priority_over_context_output(self):
    result = self._run(DEF, "-C", "3", "--stdout", "target/1")
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertTrue(result.stdout.startswith("a.py:"), result.stdout)

  def test_selection_takes_priority_over_other_incompatible_output(self):
    for arguments in (("-c",), ("-q",), ("-o",), ("-Oviewer",)):
      with self.subTest(arguments=arguments):
        result = self._run(DEF, *arguments, "--stdout", "target/1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("a.py:"), result.stdout)

  def test_json_takes_priority_over_aggregate_and_context_output(self):
    result = self._run(GG, "--json", "-lC", "2", "target")
    self.assertEqual(result.returncode, 0, result.stderr)
    records = [json.loads(line) for line in result.stdout.splitlines()]
    self.assertGreaterEqual(len(records), 2)
    self.assertIsInstance(records[0]["line"], int)
    self.assertIn("target", records[0]["text"])

  def test_human_and_strict_selection(self):
    first = self._run(DEF, "target/0", "--stdout")
    self.assertEqual(first.returncode, 0)
    self.assertTrue(first.stdout.startswith("a.py:"), first.stdout)

    last = self._run(DEF, "target/999", "--stdout")
    self.assertEqual(last.returncode, 0)
    self.assertTrue(last.stdout.startswith("b.py:"), last.stdout)

    strict = self._run(
      DEF, "--pattern", "target", "--show", "999")
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
    result = self._run(DEF, "target/1")
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
