from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from itertools import product
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lib.git_search import ContextBlock, SearchResult, GrepOutputMode, present_results, ResultAction
from lib.git_search_args import _split_leading_git_options, ArgumentIterator, make_output_plan, prepare_occurrence_options
from gg import ImportScanner, PythonImportScanner, _TYPESCRIPT_MULTILINE_IMPORT, _definition_occurrences, _filter_occurrence_contexts, _filter_occurrence_matches, _first_occurrences_by_line, _git_has_buggy_columns, _non_import_occurrences, _occurrences_by_line, _quoted_occurrences, _update_typescript_import_state


def _result_action(*, index=None, json=False, clamp_index=False):
  return ResultAction(
    index=index,
    clamp_index=clamp_index,
    json=json,
    open_result=False,
    stdout=True,
    tool=None,
  )


def _ranges(*occurrences):
  return tuple(
    (occurrence.column - 1, occurrence.column - 1 + len(occurrence.text))
    for occurrence in occurrences
  )


class GGUnitTest(unittest.TestCase):
  def setUp(self):
    self.temporary_directory = tempfile.TemporaryDirectory()
    self.fixture_root = Path(self.temporary_directory.name)

  def tearDown(self):
    self.temporary_directory.cleanup()

  def _write(self, filename, contents):
    (self.fixture_root / filename).write_text(contents)

  def _non_import_occurrences(self, match, occurrences, patterns):
    with ImportScanner() as import_scanner, PythonImportScanner() as python_import_scanner:
      return _non_import_occurrences(
        match, _ranges(*occurrences), patterns, import_scanner, python_import_scanner)

  def test_python_import_scanner_classifies_complete_multiline_import_forms(self):
    with PythonImportScanner() as scanner:
      for one_name_per_line, trailing_comma, closing_paren_on_name_line in product((False, True), repeat=3):
        with self.subTest(
          one_name_per_line=one_name_per_line,
          trailing_comma=trailing_comma,
          closing_paren_on_name_line=closing_paren_on_name_line,
        ):
          target = "SelectedBinding"
          if one_name_per_line:
            imported_names = f"  FirstBinding,\n  {target}"
            target_line = 3
          else:
            imported_names = f"  FirstBinding, {target}"
            target_line = 2
          if trailing_comma:
            imported_names += ","
          if closing_paren_on_name_line:
            imported_names += ")"
          else:
            imported_names += "\n)"

          source = f"from package import (\n{imported_names}\n"
          compile(source, "<multiline-import>", "exec")
          filename = f"variant_{int(one_name_per_line)}_{int(trailing_comma)}_{int(closing_paren_on_name_line)}.py"
          self._write(filename, source)

          self.assertTrue(scanner.classify(str(self.fixture_root / filename), target_line))

  def test_python_import_scanner_reuses_incremental_parser(self):
    self._write("multiline_import.py", """\
from package import (
  ImportedMarker)

ImportedMarker()
""")
    path = str(self.fixture_root / "multiline_import.py")
    with PythonImportScanner() as scanner:
      self.assertTrue(scanner.classify(path, 1))
      self.assertTrue(scanner.classify(path, 2))
      self.assertFalse(scanner.classify(path, 4))

  def test_python_import_scanner_classifies_unfinished_import_as_import(self):
    self._write("unfinished.py", "from package import (\n  Member,\n  Other,\n")
    with PythonImportScanner() as scanner:
      self.assertTrue(scanner.classify(str(self.fixture_root / "unfinished.py"), 2))

  def test_import_scanner_returns_unknown_if_requested_line_was_not_read(self):
    path = self.fixture_root / "incomplete.ts"
    self._write(path.name, "import {\n  MissingSymbol,\n")
    with ImportScanner() as scanner:
      self.assertTrue(scanner.classify(str(path), 2, _update_typescript_import_state))
      self.assertIsNone(scanner.classify(str(path), 20, _update_typescript_import_state))

  def test_import_scanner_restarts_at_lookback_window_after_large_gap(self):
    path = self.fixture_root / "long_import.ts"
    self._write(path.name, """\
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
    path = self.fixture_root / "single_import.ts"
    self._write(path.name, """\
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
    path = self.fixture_root / "module.coffee"
    self._write(path.name, """\
{ExistingBinding} = require "dependency"
IndependentBinding
""")
    match = SearchResult(str(path), 2, 1, b"IndependentBinding")
    occurrence = SearchResult(str(path), 2, 1, b"IndependentBinding")
    self.assertEqual(self._non_import_occurrences(
      match, (occurrence,), ("IndependentBinding",)), ((0, 18),))

  def test_go_import_filter(self):
    path = self.fixture_root / "module.go"
    self._write(path.name, """\
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

    def non_import_occurrences(line, text, name):
      encoded_name = name.encode()
      match = SearchResult(str(path), line, 1, text)
      occurrence = SearchResult(str(path), line, text.index(encoded_name) + 1, encoded_name)
      return _non_import_occurrences(
        match, _ranges(occurrence), (name,), import_scanner, python_import_scanner)

    with ImportScanner() as import_scanner, PythonImportScanner() as python_import_scanner:
      self.assertFalse(non_import_occurrences(3, b'import SingleAlias "module/one"', "SingleAlias"))
      self.assertFalse(non_import_occurrences(6, b'  GroupAlias "module/two"', "GroupAlias"))
      self.assertFalse(non_import_occurrences(7, b'  _ "module/driver"', "driver"))
      self.assertFalse(non_import_occurrences(8, b'  . "module/helpers"', "helpers"))
      self.assertTrue(non_import_occurrences(11, b"var Result = GroupAlias", "GroupAlias"))

  def test_import_filter_ignores_color_codes(self):
    match = SearchResult(
      "module.py", 1, 1, b"\x1b[1;31mimport\x1b[m dependency")
    occurrence = SearchResult("module.py", 1, 8, b"dependency")
    self.assertFalse(self._non_import_occurrences(match, (occurrence,), ("dependency",)))

  def test_occurrence_import_filter_keeps_mixed_lines(self):
    match = SearchResult(
      "module.py", 1, 1, b"import package; package.run()")
    imported = SearchResult("module.py", 1, 8, b"package")
    used = SearchResult("module.py", 1, 17, b"package")
    self.assertEqual(self._non_import_occurrences(match, (imported,), ("package",)), ())
    self.assertEqual(self._non_import_occurrences(match, (imported, used), ("package",)), ((16, 23),))

  def test_import_filter_checks_patterns_for_the_matched_language(self):
    python_match = SearchResult("module.py", 1, 1, b"import dependency")
    python_occurrence = SearchResult("module.py", 1, 8, b"dependency")
    coffee_match = SearchResult("module.coffee", 1, 1, b"{dependency} = require 'package'")
    coffee_occurrence = SearchResult("module.coffee", 1, 2, b"dependency")

    self.assertTrue(self._non_import_occurrences(
      python_match, (python_occurrence,), ("import dependency",)))
    self.assertFalse(self._non_import_occurrences(
      python_match, (python_occurrence,), ("{dependency} = require",)))
    self.assertTrue(self._non_import_occurrences(
      coffee_match, (coffee_occurrence,), ("{dependency} = require",)))
    self.assertFalse(self._non_import_occurrences(
      coffee_match, (coffee_occurrence,), ("import dependency",)))

  def test_quoted_occurrences_use_byte_columns_and_include_quotes(self):
    text = b'\xc3\xa9 "marker" and \'value\' plus plain'
    match = SearchResult("module.py", 1, 1, text)
    double_quoted = SearchResult("module.py", 1, 5, b"marker")
    single_quoted = SearchResult("module.py", 1, 18, b"value")
    unquoted = SearchResult("module.py", 1, 30, b"plain")
    self.assertEqual(_quoted_occurrences(match, _ranges(double_quoted, single_quoted, unquoted)), ((3, 11), (16, 23)))

  def test_definition_filter_checks_the_occurrence_position(self):
    match = SearchResult("module.py", 1, 1, b"def selected(argument): return selected")
    name = SearchResult("module.py", 1, 5, b"selected")
    argument = SearchResult("module.py", 1, 14, b"argument")
    body = SearchResult("module.py", 1, 32, b"selected")
    self.assertTrue(_definition_occurrences(match, _ranges(name)))
    self.assertFalse(_definition_occurrences(match, _ranges(argument, body)))

  def test_python_definition_occurrences_include_definition_syntax(self):
    match = SearchResult("module.py", 7, 18, b"  async def load_resource(argument):")
    name = SearchResult("module.py", 7, 18, b"resource")
    argument = SearchResult("module.py", 7, 27, b"argument")

    self.assertEqual(
      _definition_occurrences(match, _ranges(name)),
      ((2, 25),),
    )
    self.assertEqual(_definition_occurrences(match, _ranges(argument)), ())

  def test_go_definition_occurrences_include_definition_syntax(self):
    cases = (
      (b"  func (item *Store[T]) LoadItem() {}", b"Item", b"func (item *Store[T]) LoadItem"),
      (b"\ttype Bucket[T any] struct{}", b"Bucket", b"type Bucket"),
      (b"  var current *Bucket[int]", b"current", b"var current"),
      (b"  value := construct()", b"value", b"value :="),
    )
    for text, occurrence_text, definition_text in cases:
      with self.subTest(text=text):
        occurrence = SearchResult("module.go", 7, text.index(occurrence_text) + 1, occurrence_text)
        match = SearchResult("module.go", 7, occurrence.column, text)
        head = text.index(definition_text)
        self.assertEqual(_definition_occurrences(match, _ranges(occurrence)), ((head, head + len(definition_text)),))

    text = b"func Build() { Build() }"
    usage = SearchResult("module.go", 8, text.rindex(b"Build") + 1, b"Build")
    match = SearchResult("module.go", 8, usage.column, text)
    self.assertEqual(_definition_occurrences(match, _ranges(usage)), ())

  def test_definition_occurrences_select_syntax_by_filename(self):
    cases = (
      ("module.py", b"class Record(Base):", b"Record", b"class Record"),
      ("module.ts", b"export const record = value", b"record", b"export const record"),
      ("module.go", b"type Record struct{}", b"Record", b"type Record"),
    )
    for path, text, occurrence_text, definition_text in cases:
      with self.subTest(path=path):
        occurrence = SearchResult(path, 4, text.index(occurrence_text) + 1, occurrence_text)
        match = SearchResult(path, 4, occurrence.column, text)
        head = text.index(definition_text)
        self.assertEqual(_definition_occurrences(match, _ranges(occurrence)), ((head, head + len(definition_text)),))

    match = SearchResult("module.txt", 4, 7, b"class Record(Base):")
    occurrence = SearchResult("module.txt", 4, 7, b"Record")
    self.assertEqual(_definition_occurrences(match, _ranges(occurrence)), ())

  def test_definition_filter_handles_classes_async_functions_and_partial_matches(self):
    cases = (
      (b"class ServiceFactory(Base):", 14, b"Factory"),
      (b"  async def load_resource():", 13, b"load"),
    )
    for text, column, occurrence_text in cases:
      with self.subTest(text=text):
        match = SearchResult("module.py", 1, 1, text)
        occurrence = SearchResult("module.py", 1, column, occurrence_text)
        self.assertTrue(_definition_occurrences(match, _ranges(occurrence)))

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
        self.assertTrue(_definition_occurrences(match, _ranges(occurrence)))

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
        self.assertTrue(_definition_occurrences(match, _ranges(occurrence)))

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
        self.assertTrue(_definition_occurrences(match, _ranges(occurrence)))

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
        self.assertFalse(_definition_occurrences(match, _ranges(occurrence)))

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
        self.assertFalse(_definition_occurrences(match, _ranges(occurrence)))

  def test_definition_filter_is_language_aware(self):
    match = SearchResult("module.txt", 1, 1, b"def selected():")
    occurrence = SearchResult("module.txt", 1, 5, b"selected")
    self.assertFalse(_definition_occurrences(match, _ranges(occurrence)))

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
      list(_filter_occurrence_matches(results, _occurrences_by_line(occurrences), _definition_occurrences)),
      [SearchResult("module.py", 1, 5, b"def selected():", occurrence_ranges=((0, 12),))],
    )

  def test_git_version_controls_column_workaround(self):
    cases = (
      ("git version 2.34.1", True),
      ("2.54.9", True),
      ("git version 2.55.0", False),
      ("2.55.1.windows.1", False),
      ("3.0", False),
      ("", False),
      ("unknown", False),
    )
    for version, expected in cases:
      with self.subTest(version=version), patch.dict(os.environ, {"GG_GIT_VERSION": version}):
        self.assertEqual(_git_has_buggy_columns(), expected)
    with patch.dict(os.environ, {}, clear=True):
      self.assertFalse(_git_has_buggy_columns())

  def test_occurrence_filters_can_keep_only_first_match_per_line(self):
    results = (
      SearchResult("module.py", 1, 1, b"a a"),
      SearchResult("module.py", 2, 1, b"b b"),
      SearchResult("other.py", 2, 1, b"c c"),
    )
    occurrences = (
      SearchResult("module.py", 1, 1, b"a"),
      SearchResult("module.py", 1, 3, b"a"),
      SearchResult("module.py", 2, 1, b"b"),
      SearchResult("module.py", 2, 3, b"b"),
      SearchResult("other.py", 2, 1, b"c"),
      SearchResult("other.py", 2, 3, b"c"),
    )
    extractor = lambda match, ranges: ranges
    for group_occurrences, expected_ranges in (
      (_first_occurrences_by_line, ((0, 1),)),
      (_occurrences_by_line, ((0, 1), (2, 3))),
    ):
      with self.subTest(group_occurrences=group_occurrences.__name__):
        filtered = list(_filter_occurrence_matches(results, group_occurrences(occurrences), extractor))
        self.assertEqual([result.occurrence_ranges for result in filtered], [expected_ranges] * 3)
        blocks = (ContextBlock("module.py", results[:2]), ContextBlock("other.py", results[2:]))
        filtered_blocks = list(_filter_occurrence_contexts(blocks, group_occurrences(occurrences), extractor))
        self.assertEqual(
          [result.occurrence_ranges for block in filtered_blocks for result in block.matches],
          [expected_ranges] * 3,
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
    def extractor(match, line_occurrences):
      checked_lines.append(match.line)
      return _definition_occurrences(match, line_occurrences)

    self.assertEqual(
      list(_filter_occurrence_contexts(blocks, _occurrences_by_line(occurrences), extractor)),
      [
        ContextBlock("module.py", (
          SearchResult("module.py", 1, 5, b"def first():", occurrence_ranges=((0, 9),)),
          SearchResult("module.py", 2, 3, b"first()"),
        )),
        ContextBlock("module.py", (
          SearchResult("module.py", 8, 15, b"          def second():", occurrence_ranges=((10, 20),)),
        )),
      ],
    )
    self.assertEqual(checked_lines, [1, 2, 8])

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

  def test_occurrence_options_append_crop_matches_option(self):
    cases = (
      ((), ("-o",)),
      (("-i",), ("-i", "-o")),
      (("-A2", "-n"), ("-n", "-o")),
    )
    for arguments, expected in cases:
      with self.subTest(arguments=arguments):
        self.assertEqual(tuple(prepare_occurrence_options(arguments)), expected)

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
    self.assertFalse(default.crop_matches)

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

  def test_output_plan_tracks_crop_matches(self):
    plan, arguments = make_output_plan(("-io", "--only-matching"))
    self.assertTrue(plan.crop_matches)
    self.assertEqual(arguments, ("-i",))

    plan, arguments = make_output_plan(("-o", "--no-only-matching"))
    self.assertFalse(plan.crop_matches)
    self.assertEqual(arguments, ())

    plan, arguments = make_output_plan(("-o",), _result_action(json=True))
    self.assertTrue(plan.crop_matches)
    self.assertEqual(arguments, ())

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
      "--only-matching", "--no-only-matching",
    ))
    self.assertIs(plan.mode, GrepOutputMode.MATCHES)
    self.assertFalse(plan.heading)
    self.assertEqual(plan.color, "never")
    self.assertFalse(plan.quiet)
    self.assertFalse(plan.crop_matches)

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

  def test_present_results_accepts_iterators(self):
    plan, _ = make_output_plan(())
    records = (
      SearchResult("first.py", 2, 3, b"first match"),
      SearchResult("second.py", 5, 7, b"second match"),
    )

    stdout = StringIO()
    with redirect_stdout(stdout):
      status = present_results(iter(records), plan, _result_action(json=True), program="search")
    self.assertEqual(status, 0)
    self.assertEqual(
      [record["path"] for record in map(json.loads, stdout.getvalue().splitlines())],
      ["first.py", "second.py"],
    )

    stdout = StringIO()
    with redirect_stdout(stdout):
      status = present_results(
        iter(records), plan, _result_action(index=99, json=True, clamp_index=True), program="search")
    self.assertEqual(status, 0)
    self.assertEqual(json.loads(stdout.getvalue())["path"], "second.py")

    stderr = StringIO()
    with redirect_stderr(stderr):
      status = present_results(
        iter(records), plan, _result_action(index=99, json=True), program="search")
    self.assertEqual(status, 2)
    self.assertIn("outside the result range", stderr.getvalue())

    stdout = StringIO()
    with redirect_stdout(stdout):
      status = present_results(iter(()), plan, _result_action(json=True), program="search")
    self.assertEqual(status, 1)
    self.assertEqual(stdout.getvalue(), "")


if __name__ == "__main__":
  unittest.main()
