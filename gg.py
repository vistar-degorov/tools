#! /usr/bin/python

import argparse
from io import BytesIO
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
_PYTHON_IMPORT = re.compile(br"\s*(?:import\b|from\b.*\bimport\b)")
_TYPESCRIPT_IMPORT = re.compile(br"\s*import\b")
_COFFEE_IMPORT = re.compile(br"} *= *require\b")
_COMMA_LIST_OF_NAMES = re.compile(
  br"\s*(?:[(,]\s*)?"
  br"\w+(?:\s*,\s*\w+)*"
  br"\s*,?\s*\)?\s*"
)

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


def _has_non_import(match, occurrences, patterns, import_scanner=None, python_import_scanner=None):
  extension = Path(match.path).suffix.lower()
  if extension == ".py":
    find_import = _PYTHON_IMPORT.match
    pattern_has_import = _IMPORT_PATTERN.search
  elif extension in (".ts", ".tsx"):
    find_import = _TYPESCRIPT_IMPORT.match
    pattern_has_import = _IMPORT_PATTERN.search
  elif extension == ".coffee":
    find_import = _COFFEE_IMPORT.search
    pattern_has_import = _COFFEE_IMPORT_PATTERN.search
  else:
    return True

  if any(pattern_has_import(pattern) is not None for pattern in patterns):
    return True

  text = remove_color_codes(match.text)

  if _COMMA_LIST_OF_NAMES.fullmatch(text) is not None:
    if extension == ".py":
      if python_import_scanner is None:
        return True
      return python_import_scanner.classify(match.path, match.line) is not True

    if import_scanner is None:
      return True
    return import_scanner.classify(match.path, match.line, find_import) is not True

  for occurrence in occurrences:
    head = occurrence.column - 1
    statement = text[text.rfind(b";", 0, head) + 1:]
    if find_import(statement) is None:
      return True
  return False


def _is_quoted(text: bytes, head: int, tail: int) -> bool:
  if text[head] not in b'\'"':
    head = max(0, head - 1)
  if text[tail - 1] not in b'\'"':
    tail = min(tail + 1, len(text))
  return tail - head > 1 and (char := text[head]) == text[tail - 1] and char in b'\'"'


def _occurrences_by_line(occurrences):
  for _, line_occurrences in groupby(occurrences, key=lambda occ: (occ.path, occ.line)):
    yield tuple(line_occurrences)


def _has_quoted_occurrence(match, occurrences):
  text = remove_color_codes(match.text)
  for occurrence in occurrences:
    head = occurrence.column - 1
    if _is_quoted(text, head, head + len(occurrence.text)):
      return True
  return False


def _has_definition_occurrence(match, occurrences):
  extension = Path(match.path).suffix.lower()
  if extension in (".py", ".pyi", ".pyw", ".bzl"):
    definition_patterns = (_PYTHON_DEFINITION,)
  elif extension in (".ts", ".tsx"):
    definition_patterns = (_TYPESCRIPT_DECLARATION, _TYPESCRIPT_VARIABLE)
  else:
    return False

  text = remove_color_codes(match.text)
  definition_spans = tuple(
    definition.span(1)
    for pattern in definition_patterns
    if (definition := pattern.match(text)) is not None
  )
  for occurrence in occurrences:
    occurrence_head = occurrence.column - 1
    occurrence_tail = occurrence_head + len(occurrence.text)
    if any(
      occurrence_head < definition_tail and occurrence_tail > definition_head
      for definition_head, definition_tail in definition_spans
    ):
      return True
  return False


def _filter_occurrence_matches(results, occurrences, predicate):
  for match, line_occurrences in zip(results, _occurrences_by_line(occurrences)):
    if predicate(match, line_occurrences):
      yield match


def _filter_occurrence_contexts(results, occurrences, predicate):
  occurrence_lines = iter(_occurrences_by_line(occurrences))
  for block in results:
    keep_block = False
    for match in block.matches:
      line_occurrences = next(occurrence_lines, ())
      keep_match = predicate(match, line_occurrences)
      keep_block = keep_block or keep_match
    if keep_block:
      yield block


class ImportScanner:
  def __init__(self, *, lookback=_LOOKBACK_LINES, max_gap=_MAX_GAP_LINES):
    self._lookback = lookback
    self._max_gap = max_gap
    self._path = None
    self._reader = None
    self._upcoming_line = 1
    self._import_in_progress = False

  def __enter__(self):
    return self

  def __exit__(self, *_):
    self.close()
    return False

  def close(self):
    if self._reader is not None:
      self._reader.close()
    self._reader = None

  def _open(self, path):
    self.close()
    self._path = path
    try:
      self._reader = open(path, "rb")
    except OSError:
      self._reader = None
    else:
      self._upcoming_line = 1
      self._import_in_progress = False

  def classify(self, path, line, is_import):
    if path != self._path:
      self._open(path)
    if self._reader is None:
      return None
    if line < self._upcoming_line:
      raise ValueError(f"cannot restart at already-read line {line}")

    window_start = max(1, line - self._lookback)

    if window_start > self._upcoming_line + self._max_gap:
      self._import_in_progress = False
      while self._upcoming_line < window_start:
        if not self._reader.readline():
          return None
        self._upcoming_line += 1

    for text in self._reader:
      self._upcoming_line += 1

      if self._import_in_progress:
        if _COMMA_LIST_OF_NAMES.fullmatch(text) is None:
          self._import_in_progress = False
      elif is_import(text) is not None:
          self._import_in_progress = True

      if self._upcoming_line > line:
        break

    if self._upcoming_line <= line:
      return None
    return self._import_in_progress


class PythonImportScanner:
  """Classify matched Python lines using bounded, incremental tokenization.

  None means that tokenization could not establish a safe answer. Callers
  should treat it as non-import content rather than hide a possible match.
  """

  def __init__(self, *, lookback=_LOOKBACK_LINES, max_gap=_MAX_GAP_LINES):
    self._lookback = lookback
    self._max_gap = max_gap
    self._path = None
    self._reader = None
    self._parser = None
    self._upcoming_line = 1

  def __enter__(self):
    return self

  def __exit__(self, *_):
    self.close()
    return False

  def close(self):
    if self._reader is not None:
      self._reader.close()
    self._reader = None
    self._parser = None
    self._upcoming_line = 1

  def _open(self, path):
    self.close()
    self._path = path
    try:
      self._reader = tokenize.open(path)
    except (OSError, SyntaxError, UnicodeError, LookupError):
      self._reader = None

  def readline(self):
    if text := self._reader.readline():
      self._upcoming_line += 1
    return text

  def _restart(self, line):
    if line < self._upcoming_line:
      raise ValueError(f"cannot restart at already-read line {line}")
    while self._upcoming_line < line:
      if not self.readline():
        self._parser = None
        return
    self._parser = PythonImportParser(self, line_offset=line - 1)

  def classify(self, path, line):
    """Return True for an import, False for other code, or None if uncertain."""

    if path != self._path:
      self._open(path)
    if self._reader is None:
      return None

    upcoming_line = self._upcoming_line
    window_start = max(1, line - self._lookback)

    parser = self._parser
    if parser is None or parser.failed:
      if upcoming_line > line:
        return None

      self._restart(
        window_start if window_start > upcoming_line + self._max_gap else upcoming_line
      )
    elif window_start > upcoming_line + self._max_gap:
      self._restart(window_start)

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
  patterns = options.command_patterns
  pattern_texts = options.pattern_texts
  extensions = resolve_extensions(options.ext)
  filtered_extensions = extensions
  if options.scope_arguments is not None:
    filtered_extensions = (*extensions, ".py", ".ts", ".tsx", ".coffee")
  filter_predicate = None
  if main_arg == "def":
    filter_predicate = _has_definition_occurrence
  elif main_arg in ("gq", "gqq"):
    filter_predicate = _has_quoted_occurrence
  elif not options.imports and main_arg != "gi":
    filter_predicate = _has_non_import

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

  if filter_predicate and output_plan.mode in (GrepOutputMode.MATCHES, GrepOutputMode.CONTEXT):
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

  if filter_predicate:
    if filter_predicate is _has_non_import:
      with ImportScanner() as import_scanner, PythonImportScanner() as python_import_scanner:
        predicate = lambda match, line_occurrences: _has_non_import(
          match, line_occurrences, pattern_texts, import_scanner, python_import_scanner)
        if output_plan.mode == GrepOutputMode.CONTEXT:
          results = list(_filter_occurrence_contexts(results, occurrences, predicate))
        elif output_plan.mode == GrepOutputMode.MATCHES:
          results = list(_filter_occurrence_matches(results, occurrences, predicate))
    else:
      if output_plan.mode == GrepOutputMode.CONTEXT:
        results = list(_filter_occurrence_contexts(results, occurrences, filter_predicate))
      elif output_plan.mode == GrepOutputMode.MATCHES:
        results = list(_filter_occurrence_matches(results, occurrences, filter_predicate))

  return present_results(results, output_plan, result_action, program=main_arg)


if __name__ == "__main__":
  sys.exit(main())
