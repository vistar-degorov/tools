#! /usr/bin/python

import argparse
from dataclasses import replace
from itertools import groupby
from pathlib import Path
import re
import sys
import tokenize

from lib.git_search_args import (
  add_common_search_arguments,
  parse_search_arguments,
  make_output_plan,
  resolve_extensions,
)
from lib.git_search import (
  GrepOutputMode,
  remove_color_codes,
  build_search_command,
  build_occurrence_search_command,
  execute_search,
  make_result_action,
  make_search_scope,
  present_results,
)
from lib.parser import PythonImportParser

# code parser settings
_LOOKBACK_LINES = 10
_MAX_GAP_LINES = 10

# for searching command line args
_IMPORT_PATTERN = re.compile(r"\bimport\b")
_COFFEE_IMPORT_PATTERN = re.compile(r"\brequire\b")

# for searching file-content bytes
_PYTHON_DEFINITION = re.compile(br"\s*(?:(?:async\s+)?def|class)\s+([^\s(:]+)")
_TYPESCRIPT_DECLARATION = re.compile(
  br"\s*(?:(?:export|default|declare|abstract|async)\s+)*"
  br"(?:(?:class|interface|type|enum|namespace|module|const\s+enum)\s+|function\s*\*?\s+)"
  br"([A-Za-z_$][A-Za-z0-9_$]*)"
)
_TYPESCRIPT_VARIABLE = re.compile(
  br"\s*(?:(?:export|declare)\s+)*(?:const|let|var)\s+"
  br"(?!enum\b)([A-Za-z_$][A-Za-z0-9_$]*)"
)
_GO_FUNCTION = re.compile(
  br"\s*func\s+(?:\([^\r\n)]*\)\s+)?([A-Za-z_][A-Za-z0-9_]*)"
)
_GO_TYPE = re.compile(br"\s*type\s+([A-Za-z_][A-Za-z0-9_]*)")
_GO_VALUE = re.compile(br"\s*(?:var|const)\s+([A-Za-z_][A-Za-z0-9_]*)")
_GO_SHORT_VARIABLE = re.compile(br"\s*([A-Za-z_][A-Za-z0-9_]*)\s*:=")
_PYTHON_IMPORT = re.compile(br"\s*(?:import\b|from\b.*\bimport\b)")
_TYPESCRIPT_IMPORT = re.compile(br"\s*import\b")
_TYPESCRIPT_MULTILINE_IMPORT = re.compile(
  br"\s*import\s+(?:type\s+)?"
  br"(?:[A-Za-z_$][A-Za-z0-9_$]*\s*,\s*)?"
  br"\{[^}\r\n]*(?:\r?\n)?$"
)
_GO_IMPORT = re.compile(br"\s*import\b")
_GO_MULTILINE_IMPORT = re.compile(br"\s*import\s*\([^)\r\n]*(?:\r?\n)?$")
_GO_IMPORT_END = re.compile(br"\s*\)")
_COFFEE_IMPORT = re.compile(br"} *= *require\b")
_COMMA_LIST_OF_NAMES = re.compile(
  br"\s*(?:[(,]\s*)?"
  br"\w+(?:\s*,\s*\w+)*"
  br"\s*,?\s*\)?\s*"
)

def _update_typescript_import_state(text: str, import_in_progress: bool) -> bool:
  if import_in_progress:
    return _COMMA_LIST_OF_NAMES.fullmatch(text) is not None
  return _TYPESCRIPT_MULTILINE_IMPORT.match(text) is not None

def _update_go_import_state(text: str, import_in_progress: bool) -> bool:
  if import_in_progress:
    return _GO_IMPORT_END.match(text) is None
  return _GO_MULTILINE_IMPORT.match(text) is not None


def _argument_parser():
  parser = argparse.ArgumentParser(
    add_help=False,
    allow_abbrev=False,
    usage=(
      "%(prog)s [OPTIONS] [GIT_GREP_OPTIONS] [PATTERN[/N]] "
      "[REV_OR_PATH ...] [/N]"),
    description="Search source code using git grep.",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog=(
      "Examples:\n"
      "  %(prog)s PATTERN\n"
      "  %(prog)s -e PATTERN --pattern PATTERN\n"
      "  %(prog)s -A 2 PATTERN\n"
      "  %(prog)s PATTERN PATH ...\n"
      "  %(prog)s PATTERN -- PATH ...\n"
      "  %(prog)s PATTERN PATH ... /N\n"
      "\n"
      "Git grep options go before PATTERN. Remaining positional arguments "
      "are passed to git grep as revisions or paths; -- before paths is "
      "optional. Explicit operands override the default root and extension "
      "filters.\n"
      "Results include line numbers by default, so -n is unnecessary."
    ),
  )
  parser.add_argument(
    "--help", action="help", help="show this help message and exit")
  parser.add_argument(
    "-e", "--pattern", metavar="PATTERN",
    help="add a search pattern; repeat to match alternatives")
  add_common_search_arguments(parser)
  parser.add_argument(
    "--imports", action="store_true", help="include import statements in search results")
  words = parser.add_mutually_exclusive_group()
  words.add_argument(
    "-w", "--word", dest="word", action="store_true",
    help="match the pattern as a whole word")
  words.add_argument(
    "--no-word", dest="word", action="store_false",
    help="disable the default whole-word search")
  parser.set_defaults(word=None)
  return parser


def _parse_arguments(arguments=None):
  arguments = list(sys.argv[1:] if arguments is None else arguments)
  if arguments == ["-h"]:
    arguments = ["--help"]
  return parse_search_arguments(_argument_parser(), arguments, pattern_metavar="PATTERN")


def _non_import_occurrences(match, ranges, patterns, import_scanner, python_import_scanner):
  all_ranges = tuple(ranges)
  extension = Path(match.path).suffix.lower()
  if extension == ".py":
    find_import = _PYTHON_IMPORT.match
    pattern_has_import = _IMPORT_PATTERN.search
  elif extension in (".ts", ".tsx"):
    find_import = _TYPESCRIPT_IMPORT.match
    pattern_has_import = _IMPORT_PATTERN.search
  elif extension == ".go":
    find_import = _GO_IMPORT.match
    pattern_has_import = _IMPORT_PATTERN.search
  elif extension == ".coffee":
    find_import = _COFFEE_IMPORT.search
    pattern_has_import = _COFFEE_IMPORT_PATTERN.search
  else:
    return all_ranges

  # if a search pattern looks like it's searching for an import, skip filtering
  if any(pattern_has_import(pattern) is not None for pattern in patterns):
    return all_ranges

  text = remove_color_codes(match.text)

  # multiline import detection
  if extension == ".go" and import_scanner.classify(
    match.path, match.line, _update_go_import_state) is True:
    return ()

  if _COMMA_LIST_OF_NAMES.fullmatch(text) is not None:
    if extension == ".py":
      is_import = python_import_scanner.classify(match.path, match.line)

    elif extension in (".ts", ".tsx"):
      is_import = import_scanner.classify(match.path, match.line, _update_typescript_import_state)

    else:
      is_import = False

    return () if is_import is True else all_ranges

  def _filter_single_line_imports():
    for occurrence_range in all_ranges:
      head, _ = occurrence_range
      statement = text[text.rfind(b";", 0, head) + 1:]
      if find_import(statement) is None:
        yield occurrence_range

  return tuple(_filter_single_line_imports())


def _occurrences_by_line(occurrences):
  def _ranges(_occ):
    for occurrence in _occ:
      head = occurrence.column - 1
      yield head, head + len(occurrence.text)

  for _, line_occurrences in groupby(occurrences, key=lambda occ: (occ.path, occ.line)):
    yield tuple(_ranges(line_occurrences))


def _quoted_occurrences(match, ranges):
  text = remove_color_codes(match.text)
  quoted_ranges = []
  for head, tail in ranges:
    if text[head] not in b'\'"':
      head = max(0, head - 1)
    if text[tail - 1] not in b'\'"':
      tail = min(tail + 1, len(text))
    if tail - head > 1 and (char := text[head]) == text[tail - 1] and char in b'\'"':
      quoted_ranges.append((head, tail))
  return tuple(quoted_ranges)


def _definition_occurrences(match, ranges):
  extension = Path(match.path).suffix.lower()
  if extension in (".py", ".pyi", ".pyw", ".bzl"):
    patterns = (_PYTHON_DEFINITION,)
  elif extension in (".ts", ".tsx"):
    patterns = (_TYPESCRIPT_DECLARATION, _TYPESCRIPT_VARIABLE)
  elif extension == ".go":
    patterns = (_GO_FUNCTION, _GO_TYPE, _GO_VALUE, _GO_SHORT_VARIABLE)
  else:
    return ()

  text = remove_color_codes(match.text)
  for pattern in patterns:
    if (definition := pattern.match(text)) is None:
      continue

    name_head, name_tail = definition.span(1)
    if any(head < name_tail and tail > name_head for head, tail in ranges):
      definition_text = definition.group(0).lstrip()
      definition_head = definition.end() - len(definition_text)
      return ((definition_head, definition.end()),)
  return ()


def _filter_occurrence_matches(results, occurrences, extractor):
  for match, line_occurrences in zip(results, _occurrences_by_line(occurrences)):
    if occurrence_ranges := extractor(match, line_occurrences):
      yield replace(match, occurrence_ranges=occurrence_ranges)


def _filter_occurrence_contexts(results, occurrences, extractor):
  occurrence_lines = iter(_occurrences_by_line(occurrences))
  for block in results:
    keep_block = False
    lines = []
    for result in block.lines:
      if result.is_match:
        line_occurrences = next(occurrence_lines, ())
        if occurrence_ranges := extractor(result, line_occurrences):
          result = replace(result, occurrence_ranges=occurrence_ranges)
          keep_block = True
      lines.append(result)
    if keep_block:
      yield replace(block, lines=tuple(lines))


class _BaseFileScanner:
  _open_errors = (OSError,)

  def __init__(self, *, lookback=_LOOKBACK_LINES, max_gap=_MAX_GAP_LINES):
    self._lookback = lookback
    self._max_gap = max_gap
    self._path = None
    self._reader = None
    self._upcoming_line = 1
    self._reset_state()

  def __enter__(self):
    return self

  def __exit__(self, *_):
    self.close()
    return False

  def close(self):
    if self._reader is not None:
      self._reader.close()
    self._reader = None
    self._upcoming_line = 1
    self._reset_state()

  def _open_reader(self, path):
    return open(path, "rb")

  def _open(self, path):
    self.close()
    self._path = path
    try:
      self._reader = self._open_reader(path)
    except self._open_errors:
      self._reader = None

  def readline(self):
    if text := self._reader.readline():
      self._upcoming_line += 1
    return text

  def _skip_to(self, line):
    if line < self._upcoming_line:
      raise ValueError(f"cannot restart at already-read line {line}")
    while self._upcoming_line < line:
      if not self.readline():
        return False
    return True

  def _prepare(self, path, line):
    if path != self._path:
      self._open(path)
    if self._reader is None:
      return None
    return max(1, line - self._lookback)


class ImportScanner(_BaseFileScanner):
  def _reset_state(self):
    self._import_in_progress = False

  def classify(self, path, line, update_import_state):
    if (window_start := self._prepare(path, line)) is None:
      return None
    if line < self._upcoming_line:
      raise ValueError(f"cannot restart at already-read line {line}")

    if window_start > self._upcoming_line + self._max_gap:
      self._import_in_progress = False
      if not self._skip_to(window_start):
        return None

    while self._upcoming_line <= line:
      if not (text := self.readline()):
        return None

      self._import_in_progress = update_import_state(text, self._import_in_progress)

    return self._import_in_progress


class PythonImportScanner(_BaseFileScanner):
  """Classify matched Python lines using bounded, incremental tokenization.

  None means that tokenization could not establish a safe answer. Callers
  should treat it as non-import content rather than hide a possible match.
  """

  _open_errors = (OSError, SyntaxError, UnicodeError, LookupError)

  def _reset_state(self):
    self._parser = None

  def _open_reader(self, path):
    return tokenize.open(path)

  def _restart_parser_at(self, line):
    if self._skip_to(line):
      self._parser = PythonImportParser(self, line_offset=line - 1)
    else:
      self._parser = None

  def classify(self, path, line):
    """Return True for an import, False for other code, or None if uncertain."""

    if (window_start := self._prepare(path, line)) is None:
      return None

    upcoming_line = self._upcoming_line
    parser = self._parser
    if parser is None or parser.failed:
      if upcoming_line > line:
        return None

      self._restart_parser_at(
        window_start if window_start > upcoming_line + self._max_gap else upcoming_line
      )
    elif window_start > upcoming_line + self._max_gap:
      self._restart_parser_at(window_start)

    parser = self._parser
    if parser is None:
      return None

    parser.advance_through(line)
    for start, end in reversed(parser.completed_import_ranges):
      if start[0] <= line <= end[0]:
        return True
      if end[0] < line:
        break

    if parser.import_in_progress or parser.failed or parser.advanced_through < line:
      return None
    return False


def main(arguments=None):
  options = _parse_arguments(arguments)
  result_action = make_result_action(options)
  output_plan, grep_args = make_output_plan(options.grep_args, result_action)
  main_arg = Path(sys.argv[0]).name
  patterns = () if options.pattern is None else (options.pattern,)
  pattern_texts = options.pattern_texts
  extensions = resolve_extensions(options.ext)
  filtered_extensions = extensions
  if options.scope_arguments is not None:
    filtered_extensions = (*extensions, ".py", ".ts", ".tsx", ".coffee")
  range_extractor = None
  if main_arg == "def":
    range_extractor = _definition_occurrences
  elif main_arg in ("gq", "gqq"):
    range_extractor = _quoted_occurrences
  elif not options.imports and main_arg != "gi":
    range_extractor = _non_import_occurrences
  elif output_plan.crop_matches:
    range_extractor = lambda _, occ: occ

  if options.word is None:
    word_regexp = (
      options.all_patterns_known
      and bool(pattern_texts)
      and all(
        re.match(r"\w", pattern) is not None
        and re.search(r"\w$", pattern) is not None
        for pattern in pattern_texts
      )
    )
  else:
    word_regexp = options.word

  test_indicators = ["[Tt]est"]
  if {".coffee", ".ts", ".tsx"}.intersection(filtered_extensions):
    test_indicators.append("[Ss]pec")
  scope = make_search_scope(options, grep_args, extensions, test_indicators=test_indicators)
  debug_mode = options.debug or scope.debug

  if range_extractor and output_plan.mode in (GrepOutputMode.MATCHES, GrepOutputMode.CONTEXT):
    cmd = build_occurrence_search_command(
      patterns, scope.grep_args, scope.arguments,
      untracked=options.untracked,
      word_regexp=word_regexp)
    status, occurrences = execute_search(
      cmd, output_plan.with_mode(GrepOutputMode.MATCHES), program=main_arg, debug=debug_mode)
    if not debug_mode and occurrences is None:
      return status

  cmd = build_search_command(
    patterns, scope.grep_args, scope.arguments, output_plan, result_action,
    untracked=options.untracked,
    word_regexp=word_regexp,
  )
  status, results = execute_search(cmd, output_plan, program=main_arg, debug=debug_mode)
  if results is None:
    return status

  if range_extractor:
    if range_extractor is _non_import_occurrences:
      with ImportScanner() as import_scanner, PythonImportScanner() as python_import_scanner:
        extractor = lambda match, line_occurrences: _non_import_occurrences(
          match, line_occurrences, pattern_texts, import_scanner, python_import_scanner)
        if output_plan.mode == GrepOutputMode.CONTEXT:
          results = _filter_occurrence_contexts(results, occurrences, extractor)
        elif output_plan.mode == GrepOutputMode.MATCHES:
          results = _filter_occurrence_matches(results, occurrences, extractor)
    else:
      if output_plan.mode == GrepOutputMode.CONTEXT:
        results = _filter_occurrence_contexts(results, occurrences, range_extractor)
      elif output_plan.mode == GrepOutputMode.MATCHES:
        results = _filter_occurrence_matches(results, occurrences, range_extractor)

  return present_results(results, output_plan, result_action, program=main_arg)


if __name__ == "__main__":
  sys.exit(main())
