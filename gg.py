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
  parse_search_arguments,
  parse_null_results,
  remove_color_codes,
  resolve_extensions,
  resolve_setting,
  run_command,
  select_result,
  write_context,
  write_json_results,
  write_result,
)


def _argument_parser():
  root_default = resolve_setting("GG_SEARCH_IN", "./")
  extension_default = resolve_setting("GG_SEARCH_EXT", ".py")
  parser = argparse.ArgumentParser(
    allow_abbrev=False,
    usage=(
      "%(prog)s [OPTIONS] [GIT_GREP_OPTIONS] PATTERN[/N] "
      "[REV_OR_PATH ...] [/N]"),
    description="Search source code using git grep.",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog=(
      "Examples:\n"
      "  gg PATTERN\n"
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
    "--pattern", help="set the pattern explicitly; useful when it ends in /N")
  parser.add_argument(
    "--show", type=int, metavar="N",
    help="print the Nth search result in context of surrounding text")
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
  parser.add_argument(
    "--root", metavar="PATH",
    help=f"search path prefix (effective default: {root_default})")
  parser.add_argument(
    "--ext", action="append", metavar="EXT",
    help=(
      "file extension to search; may be repeated "
      f"(effective default: {extension_default})"))
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
  return parse_search_arguments(
    _argument_parser(), "pattern", "PATTERN", arguments)


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
  filtered_extensions = extensions
  if options.scope_arguments is not None:
    filtered_extensions = (*extensions, ".py", ".ts", ".tsx", ".coffee")
  import_regexes = _import_regexes(filtered_extensions)
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
  if {".coffee", ".ts", ".tsx"}.intersection(filtered_extensions):
    test_indicators.append("[Ss]pec")
  config = Config(
    options.grep_args,
    test_indicators=test_indicators,
    test_mode=options.tests or "exclude",
  )
  if options.scope_arguments is None:
    prefix = options.root or resolve_setting("GG_SEARCH_IN", "./")
    scope_arguments = ("--", *config.format(prefix, extensions))
  else:
    scope_arguments = (*options.scope_arguments, *config.mods)
  custom_output = has_output_modifiers(config.flags)
  structured = (
    options.json
    or options.show is not None
    or options.result_idx is not None
  )
  if custom_output and structured:
    options.parser.error(
      "output-formatting git grep options cannot be combined with "
      "selection or --json")

  cmd = build_command(
    (pattern,),
    config.flags,
    scope_arguments,
    structured=structured,
    untracked=options.untracked,
    word_regexp=word_regexp,
  )
  if options.debug or config.debug_mode:
    print(shlex.join(cmd))
    return 0

  completed = run_command(cmd)
  if completed.returncode != 0:
    return completed.returncode

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

  if options.show is not None or options.result_idx is not None:
    strict = options.show is not None
    index = options.show if strict else options.result_idx
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
