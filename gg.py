#! /usr/bin/python

import argparse
from pathlib import Path
import re
import shlex
import sys

from git_search import (
  Config,
  build_command,
  has_output_modifiers,
  open_search_result,
  parse_null_results,
  remove_color_codes,
  resolve_extensions,
  resolve_setting,
  run_command,
  select_result,
  split_result_idx,
  write_context,
  write_json_results,
  write_result,
)


def _argument_parser():
  parser = argparse.ArgumentParser(
    allow_abbrev=False,
    usage="%(prog)s [OPTIONS] [GIT_GREP_OPTIONS] PATTERN[/N]",
    description="Search source code using git grep.",
    epilog=(
      "PATTERN/N selects a result using forgiving legacy rules: 0 and 1 "
      "select the first result, and large values select the last. Use "
      "--pattern and --select for strict, agent-friendly selection. Git grep "
      "options may also be placed after --."
    ),
  )
  parser.add_argument(
    "--pattern", help="verbatim pattern; disables the /N shorthand")
  parser.add_argument(
    "--select", type=int, metavar="N",
    help="strictly select the one-based result N")
  parser.add_argument(
    "--tests", choices=("exclude", "include", "only"), default=None,
    help="control whether test files are searched (default: exclude)")
  parser.add_argument(
    "-t", "--include-tests", dest="tests", action="store_const",
    const="include", help="include test files")
  parser.add_argument(
    "-T", "--tests-only", dest="tests", action="store_const",
    const="only", help="search only test files")
  parser.add_argument(
    "--imports", action="store_true", help="include import statements")
  parser.add_argument(
    "-u", "--untracked", action="store_true",
    help="include untracked files while respecting .gitignore")
  parser.add_argument("--root", metavar="PATH", help="search path prefix")
  parser.add_argument(
    "--ext", action="append", metavar="EXT",
    help="file extension to search; may be repeated")
  parser.add_argument(
    "--json", action="store_true", help="write JSON Lines results")
  output = parser.add_mutually_exclusive_group()
  output.add_argument(
    "--stdout", action="store_true",
    help="print a selected result instead of opening an editor")
  output.add_argument(
    "--open", dest="open_result", action="store_true",
    help="open a selected result even when stdout is not a terminal")
  parser.add_argument("--tool", help="editor command used with --open")
  words = parser.add_mutually_exclusive_group()
  words.add_argument(
    "-w", "--word", dest="word", action="store_true",
    help="match the pattern as a whole word")
  words.add_argument(
    "--no-word", dest="word", action="store_false",
    help="disable the default whole-word search")
  parser.set_defaults(word=None)
  parser.add_argument(
    "--debug", action="store_true", help="print the git grep command")
  return parser


def _parse_arguments(arguments=None):
  parser = _argument_parser()
  arguments = list(sys.argv[1:] if arguments is None else arguments)
  if "--" in arguments:
    separator = arguments.index("--")
    wrapper_arguments = arguments[:separator]
    passthrough = arguments[separator + 1:]
  else:
    wrapper_arguments = arguments
    passthrough = []

  options, remaining = parser.parse_known_args(wrapper_arguments)
  if options.pattern is not None:
    pattern = options.pattern
    result_idx = None
    grep_args = [*remaining, *passthrough]
  else:
    if not remaining or remaining[-1] in ("+debug", "+print", "+t", "+to"):
      parser.error("PATTERN is required")
    pattern, result_idx = split_result_idx(remaining[-1])
    grep_args = [*remaining[:-1], *passthrough]

  if options.select is not None and result_idx is not None:
    parser.error("use either PATTERN/N or --select, not both")
  if options.json and options.open_result:
    parser.error("--json cannot be combined with --open")
  if options.open_result and options.select is None and result_idx is None:
    parser.error("--open requires --select or PATTERN/N")
  if pattern == "":
    parser.error("pattern cannot be empty")

  options.pattern = pattern
  options.result_idx = result_idx
  options.grep_args = grep_args
  options.parser = parser
  return options


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
  if extension == ".py":
    return re.match(
      r"\s*(?:import\b|from\b.*\bimport\b)", result.text) is not None
  if extension in (".ts", ".tsx"):
    return re.match(r"\s*import\b", result.text) is not None
  if extension == ".coffee":
    return re.search(r"} *= *require\b", result.text) is not None
  return False


def main(arguments=None):
  options = _parse_arguments(arguments)
  main_arg = Path(sys.argv[0]).name
  pattern = options.pattern
  extensions = resolve_extensions(options.ext)
  import_regexes = _import_regexes(extensions)
  include_imports = (
    options.imports
    or main_arg == "gi"
    or not _has_no_import(pattern, import_regexes)
  )

  quoted_search = main_arg in ("gq", "gqq")
  if main_arg == "gq":
    pattern = f"'{pattern}'"
  elif main_arg == "gqq":
    pattern = f'"{pattern}"'

  if options.word is None:
    word_regexp = (
      not quoted_search
      and re.match(r"\w", pattern) is not None
      and re.search(r"\w$", pattern) is not None
    )
  else:
    word_regexp = options.word

  test_indicators = ["[Tt]est"]
  if {".coffee", ".ts", ".tsx"}.intersection(extensions):
    test_indicators.append("[Ss]pec")
  config = Config(
    options.grep_args,
    test_indicators=test_indicators,
    test_mode=options.tests or "exclude",
  )
  prefix = options.root or resolve_setting("GG_SEARCH_IN", "./")
  pathspecs = config.format(prefix, extensions)
  custom_output = has_output_modifiers(config.flags)
  structured = not custom_output
  if custom_output and (
    options.json
    or options.select is not None
    or options.result_idx is not None
  ):
    options.parser.error(
      "output-formatting git grep options cannot be combined with "
      "selection or --json")

  cmd = build_command(
    (pattern,),
    config.flags,
    pathspecs,
    structured=structured,
    untracked=options.untracked,
    word_regexp=word_regexp,
  )
  if options.debug or config.debug_mode:
    print(shlex.join(cmd))
    return 0

  completed = run_command(cmd)
  if completed.returncode > 1:
    return completed.returncode
  if completed.returncode == 1:
    return 1

  if not structured:
    if include_imports:
      sys.stdout.buffer.write(completed.stdout)
      return 0
    wrote_result = False
    for line in completed.stdout.splitlines():
      text = remove_color_codes(line.decode("utf-8", "replace"))
      if _has_no_import(text, import_regexes):
        sys.stdout.buffer.write(line + b"\n")
        wrote_result = True
    return 0 if wrote_result else 1

  try:
    results = list(parse_null_results(completed.stdout))
  except ValueError as error:
    sys.stderr.write(f"gg: {error}\n")
    return 2
  if not include_imports:
    results = [result for result in results if not _is_import_result(result)]
  if not results:
    return 1

  if options.select is not None or options.result_idx is not None:
    strict = options.select is not None
    index = options.select if strict else options.result_idx
    try:
      result = select_result(results, index, clamp=not strict)
    except IndexError:
      sys.stderr.write(f"gg: result {index} is outside 1..{len(results)}\n")
      return 2

    if options.json:
      write_json_results((result,))
      return 0

    should_open = options.open_result or (
      not strict and not options.stdout and sys.stdout.isatty())
    if should_open:
      tool = options.tool
      if tool is None:
        tool = resolve_setting("GG_TOOL", "")
      return open_search_result(tool, result)
    return write_context(result)

  if options.json:
    write_json_results(results)
  else:
    for result in results:
      write_result(result)
  return 0


if __name__ == "__main__":
  sys.exit(main())
