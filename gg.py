#! /usr/bin/python

import argparse
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
      "  gg PATTERN\n"
      "  gg -e PATTERN --pattern PATTERN\n"
      "  gg -A 2 PATTERN\n"
      "  gg PATTERN PATH ...\n"
      "  gg PATTERN -- PATH ...\n"
      "  gg PATTERN PATH ... /N\n"
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
  return parse_search_arguments(
    _argument_parser(), "pattern", "PATTERN", arguments,
    pattern_options=("-e", "--pattern"))


def _import_regexes(extensions):
  regexes = []
  if {".py", ".ts", ".tsx"}.intersection(extensions):
    regexes.append(re.compile(r"\bimport "))
  if ".coffee" in extensions:
    regexes.append(re.compile(r"} *= *require\b"))
  return tuple(regexes)


def _has_no_import(text, import_regexes):
  return all(regex.search(text) is None for regex in import_regexes)


def _is_import_result(result):
  extension = Path(result.path).suffix.lower()
  text = remove_color_codes(result.text)
  if extension == ".py":
    return re.match(
      r"\s*(?:import\b|from\b.*\bimport\b)", text) is not None
  if extension in (".ts", ".tsx"):
    return re.match(r"\s*import\b", text) is not None
  if extension == ".coffee":
    return re.search(r"} *= *require\b", text) is not None
  return False


def _filter_import_results(results, mode):
  if mode is GrepOutputMode.MATCHES:
    return [result for result in results if not _is_import_result(result)]
  if mode is GrepOutputMode.CONTEXT:
    return [
      block for block in results
      if not all(_is_import_result(result) for result in block.matches)
    ]
  return results


def _is_quoted(text, head, tail):
  if text[head] not in '\'"':
    head = max(0, head - 1)
  if text[tail - 1] not in '\'"':
    tail = min(tail + 1, len(text))
  return tail - 1 > head and (char := text[head]) == text[tail - 1] and char in '\'"'


def _quoted_results(results, occurrences):
  current_line = None
  matches = iter(results)

  for occurrence in occurrences:
    if (occurrence.path, occurrence.line) == current_line:
      if skip_to_next_match:
        # skip the remaining occurrences on the current line
        continue

    else:
      # feed the next matched line
      try:
        current_match = next(matches)
        current_line = (current_match.path, current_match.line)
      except StopIteration:
        break

      skip_to_next_match = False
      current_line_text = remove_color_codes(current_match.text)

    head = occurrence.column - 1
    tail = head + len(occurrence.text)
    if _is_quoted(current_line_text, head, tail):
      skip_to_next_match = True
      yield current_match


def _quoted_results_with_context(results, occurrences):
  current_line = None
  matches = iter(())
  blocks = iter(results)

  for occurrence in occurrences:
    if (occurrence.path, occurrence.line) == current_line:
      if skip_to_next_match:
        # skip the remaining occurrences on the current line
        continue

    else:
      # feed the next matched line
      try:
        current_match = next(matches)
        current_line = (current_match.path, current_match.line)
        if skip_to_next_match:
          continue

      except StopIteration:
        try:
          current_block = next(blocks)
          matches = iter(current_block.matches)
        except StopIteration:
          break
        current_match = next(matches)
        current_line = (current_match.path, current_match.line)

      skip_to_next_match = False
      current_line_text = remove_color_codes(current_match.text)

    head = occurrence.column - 1
    tail = head + len(occurrence.text)
    if _is_quoted(current_line_text, head, tail):
      skip_to_next_match = True
      yield current_block


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
  import_regexes = _import_regexes(filtered_extensions)
  include_imports = (
    options.imports
    or main_arg == "gi"
    or any(not _has_no_import(pattern, import_regexes) for pattern in pattern_texts)
  )

  quoted_search = main_arg in ("gq", "gqq")

  if options.word is None:
    word_regexp = (
      options.all_patterns_known
      and bool(pattern_texts)
      and not quoted_search
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

  occurrences = []
  if output_plan.mode in (GrepOutputMode.MATCHES, GrepOutputMode.CONTEXT):
    cmd = build_occurrence_search_command(
      patterns, scope.grep_args, scope.arguments,
      untracked=options.untracked,
      word_regexp=word_regexp)
    status, occurrences = execute_search(cmd, output_plan.with_mode(GrepOutputMode.MATCHES), program=main_arg, debug=debug_mode)
    if occurrences is None:
      return status

  cmd = build_search_command(
    patterns, scope.grep_args, scope.arguments, output_plan, result_action,
    untracked=options.untracked,
    word_regexp=word_regexp,
  )
  status, results = execute_search(cmd, output_plan, program=main_arg, debug=debug_mode)
  if results is None:
    return status

  if quoted_search:
    if output_plan.mode == GrepOutputMode.CONTEXT:
      results = list(_quoted_results_with_context(results, occurrences))
    else:
      results = list(_quoted_results(results, occurrences))

  if not include_imports:
    results = _filter_import_results(results, output_plan.mode)
  return present_results(results, output_plan, result_action, program=main_arg)


if __name__ == "__main__":
  sys.exit(main())
