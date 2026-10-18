#! /usr/bin/python

import argparse
from itertools import groupby
from pathlib import Path
import re
import sys

from git_search_args import (
  add_common_search_arguments,
  parse_search_arguments,
  prepare_output,
  resolve_extensions,
)
from git_search import (
  GrepOutputMode,
  remove_color_codes,
)
from git_search_cli import (
  build_search_command,
  build_occurrence_search_command,
  execute_search,
  make_result_action,
  prepare_search_scope,
  present_results,
)


_PYTHON_DEFINITION = re.compile(br"\s*(?:(?:async\s+)?def|class)\s+([^\s(:]+)")
_IMPORT_PATTERN = re.compile(r"\bimport ")
_COFFEE_IMPORT_PATTERN = re.compile(r"} *= *require\b")
_PYTHON_IMPORT = re.compile(br"\s*(?:import\b|from\b.*\bimport\b)")
_TYPESCRIPT_IMPORT = re.compile(br"\s*import\b")
_COFFEE_IMPORT = re.compile(br"} *= *require\b")


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


def _has_non_import(match, occurrences, patterns):
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
  if Path(match.path).suffix.lower() not in (".py", ".pyi", ".pyw"):
    return False

  text = remove_color_codes(match.text)
  definition = _PYTHON_DEFINITION.match(text)
  if definition is None:
    return False

  definition_head, definition_tail = definition.span(1)
  return any(
    (occurrence_head := occurrence.column - 1) < definition_tail
    and occurrence_head + len(occurrence.text) > definition_head
    for occurrence in occurrences
  )


def _filter_occurrence_matches(results, occurrences, keep):
  for match, line_occurrences in zip(results, _occurrences_by_line(occurrences)):
    if keep(match, line_occurrences):
      yield match


def _filter_occurrence_contexts(results, occurrences, keep):
  occurrence_lines = iter(_occurrences_by_line(occurrences))
  for block in results:
    keep_block = False
    for match in block.matches:
      line_occurrences = next(occurrence_lines, ())
      if not keep_block and keep(match, line_occurrences):
        keep_block = True
    if keep_block:
      yield block


def main(arguments=None):
  options = _parse_arguments(arguments)
  output_plan, grep_args = prepare_output(options.grep_args)
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
    filter_predicate = lambda match, occurrences: _has_non_import(
      match, occurrences, pattern_texts)

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
  result_action = make_result_action(options, output_plan)
  scope = prepare_search_scope(options, grep_args, extensions, test_indicators=test_indicators)
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
    if output_plan.mode == GrepOutputMode.CONTEXT:
      results = list(_filter_occurrence_contexts(results, occurrences, filter_predicate))
    elif output_plan.mode == GrepOutputMode.MATCHES:
      results = list(_filter_occurrence_matches(results, occurrences, filter_predicate))

  return present_results(results, output_plan, result_action, program=main_arg)


if __name__ == "__main__":
  sys.exit(main())
