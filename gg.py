#! /usr/bin/python

import argparse
from pathlib import Path
import re
import shlex
import sys
from types import SimpleNamespace

from git_search_args import (
  Config,
  GIT_GREP_OPTIONS_WITH_VALUE,
  has_output_modifiers,
  parse_search_arguments,
  resolve_extensions,
  resolve_setting,
)
from git_search import (
  OutputPlan,
  SearchResult,
  StructuredOutputMode,
  build_command,
  open_search_result,
  output_uses_color,
  parse_structured_records,
  remove_color_codes,
  run_command,
  select_result,
  write_context,
  write_json_results,
  write_structured_records,
)


_CONTEXT_SETTINGS = {
  "-A": ("after_context",),
  "--after-context": ("after_context",),
  "-B": ("before_context",),
  "--before-context": ("before_context",),
  "-C": ("before_context", "after_context"),
  "--context": ("before_context", "after_context"),
}

_LONG_BOOLEAN_SETTINGS = {
  "--files-with-matches": "files_with_matches",
  "--name-only": "files_with_matches",
  "--files-without-match": "files_without_match",
  "--count": "count",
  "--show-function": "show_function",
  "--function-context": "function_context",
  "--line-number": "line_numbers",
  "--column": "show_column_number",
  "--heading": "heading",
  "--break": "break_between_files",
  "--null": "null_terminate_filenames",
  "--quiet": "quiet",
}

_SHORT_SETTINGS = {
  "h": ("show_filename", False),
  "H": ("show_filename", True),
  "n": ("line_numbers", True),
  "c": ("count", True),
  "l": ("files_with_matches", True),
  "L": ("files_without_match", True),
  "p": ("show_function", True),
  "W": ("function_context", True),
  "z": ("null_terminate_filenames", True),
  "q": ("quiet", True),
}

_PRESENTATION_LONG_OPTIONS = {
  "--line-number", "--column", "--heading", "--break", "--null", "--color",
}

_PRESENTATION_SHORT_OPTIONS = frozenset("hHnz")
_COLOR_VALUES = {"always", "auto", "never"}


def _filter_short_option(argument, settings):
  short_options = argument[1:]
  if not short_options:
    return argument
  elif short_options.isdigit():
    settings.before_context = True
    settings.after_context = True
    return argument

  git_options = ""
  for index, short_option in enumerate(short_options):
    if short_option in "efmO":
      git_options += short_options[index:]
      break
    if context_settings := _CONTEXT_SETTINGS.get("-" + short_option):
      for setting in context_settings:
        setattr(settings, setting, True)
      git_options += short_options[index:]
      break
    if update := _SHORT_SETTINGS.get(short_option):
      setting, setting_value = update
      setattr(settings, setting, setting_value)
    if short_option not in _PRESENTATION_SHORT_OPTIONS:
      git_options += short_option

  return "-" + git_options if git_options else None


def _filter_arguments(arguments, settings):
  arguments = iter(arguments)
  for argument in arguments:
    option_value = None
    if argument in GIT_GREP_OPTIONS_WITH_VALUE:
      option_value = next(arguments, None)

    option, assignment, assigned_value = argument.partition("=")
    enabled = True
    if option.startswith("--no-"):
      option = f"--{option.removeprefix('--no-')}"
      enabled = False

    if context_settings := _CONTEXT_SETTINGS.get(option):
      for setting in context_settings:
        setattr(settings, setting, enabled)
    elif setting := _LONG_BOOLEAN_SETTINGS.get(option):
      setattr(settings, setting, enabled)
    elif option == "--color":
      settings.color = (
        assigned_value if enabled and assignment
        else "always" if enabled
        else "never"
      )
    elif argument.startswith("-") and not argument.startswith("--"):
      argument = _filter_short_option(argument, settings)

    valid_presentation_option = (
      option in _PRESENTATION_LONG_OPTIONS
      and (
        not assignment
        or option == "--color" and enabled and assigned_value in _COLOR_VALUES
      )
    )
    if argument is not None and not valid_presentation_option:
      yield argument
      if option_value is not None:
        yield option_value


def _prepare_output(arguments):
  settings = SimpleNamespace(
    show_filename=True,
    line_numbers=None, # can be implicit
    show_column_number=False,
    heading=False,
    break_between_files=False,
    null_terminate_filenames=False,
    color=None,
    count=False,
    files_with_matches=False,
    files_without_match=False,
    before_context=False,
    after_context=False,
    show_function=False,
    function_context=False,
    quiet=False,
  )
  arguments = tuple(_filter_arguments(arguments, settings))

  if settings.files_with_matches or settings.files_without_match:
    mode = StructuredOutputMode.PATHS
    settings.show_filename = True
  elif settings.count:
    mode = StructuredOutputMode.COUNTS
  elif any((
    settings.before_context, settings.after_context,
    settings.show_function, settings.function_context)):
    mode = StructuredOutputMode.CONTEXT
  else:
    mode = StructuredOutputMode.MATCHES

  if mode in (StructuredOutputMode.PATHS, StructuredOutputMode.COUNTS):
    settings.line_numbers = False
    settings.show_column_number = False
  elif settings.line_numbers is None:
    settings.line_numbers = settings.show_filename

  return OutputPlan(
    mode=mode,
    show_filename=settings.show_filename,
    show_line_number=settings.line_numbers,
    show_column_number=settings.show_column_number,
    heading=settings.heading,
    break_between_files=settings.break_between_files,
    null_terminate_filenames=settings.null_terminate_filenames,
    color=settings.color,
    quiet=settings.quiet,
  ), arguments


def _make_output_plan(arguments):
  return _prepare_output(arguments)[0]


def _argument_parser():
  root_default = resolve_setting("GG_SEARCH_IN", "./")
  extension_default = resolve_setting("GG_SEARCH_EXT", ".py")
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
  parser.add_argument(
    "--show", type=int, metavar="N",
    help="print the Nth search result in context of surrounding text")
  parser.add_argument(
    "--tests", choices=("exclude", "include", "only"), default=None,
    help="control whether test files are searched (default: exclude)")
  test_shortcuts = parser.add_mutually_exclusive_group()
  test_shortcuts.add_argument(
    "-t", "--include-tests", dest="tests", action="store_const",
    const="include", help="include test files")
  test_shortcuts.add_argument(
    "-T", "--tests-only", dest="tests", action="store_const",
    const="only", help="search only test files")
  parser.add_argument(
    "--imports", action="store_true", help="include import statements in search results")
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
  if mode is StructuredOutputMode.MATCHES:
    return [result for result in results if not _is_import_result(result)]
  if mode is StructuredOutputMode.CONTEXT:
    return [
      block for block in results
      if not all(_is_import_result(result) for result in block.matches)
    ]
  return results


def main(arguments=None):
  options = _parse_arguments(arguments)
  output_plan, structured_grep_args = _prepare_output(options.grep_args)
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
    or any(
      not _has_no_import(pattern, import_regexes)
      for pattern in pattern_texts
    )
  )

  quoted_search = main_arg in ("gq", "gqq")
  if main_arg == "gq":
    patterns = tuple(f"'{pattern}'" for pattern in patterns)
  elif main_arg == "gqq":
    patterns = tuple(f'"{pattern}"' for pattern in patterns)

  if options.word is None:
    word_regexp = (
      options.patterns_complete
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
  selecting_result = options.show is not None or options.result_idx is not None
  machine_output = options.json or selecting_result

  config = Config(
    structured_grep_args,
    test_indicators=test_indicators,
    test_mode=options.tests or "exclude",
  )
  if options.scope_arguments is None:
    prefix = options.root or resolve_setting("GG_SEARCH_IN", "./")
    scope_arguments = ("--", *config.format(prefix, extensions))
  else:
    scope_arguments = (*options.scope_arguments, *config.mods)
  custom_output = has_output_modifiers(options.grep_args)
  selecting_filename = (
    selecting_result and output_plan.mode is StructuredOutputMode.PATHS)
  if custom_output and machine_output and not (
    selecting_filename and not options.json
  ):
    options.parser.error(
      "output-formatting git grep options cannot be combined with "
      "selection or --json")

  cmd = build_command(
    patterns,
    config.flags,
    scope_arguments,
    structured=True,
    structured_color=(
      not machine_output and output_uses_color(output_plan, sys.stdout)),
    untracked=options.untracked,
    word_regexp=word_regexp,
  )
  if options.debug or config.debug_mode:
    print(shlex.join(cmd))
    return 0

  completed = run_command(cmd)
  if completed.returncode != 0:
    return completed.returncode
  if output_plan.quiet:
    return 0

  try:
    results = list(parse_structured_records(
      completed.stdout, output_plan.mode))
  except ValueError as error:
    sys.stderr.write(f"gg: {error}\n")
    return 2
  if not include_imports:
    results = _filter_import_results(results, output_plan.mode)
  if not results:
    return 1

  if selecting_filename:
    results = [SearchResult(result.path, None, None, "") for result in results]

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
    write_structured_records(results, output_plan, sys.stdout.buffer)
  return 0


if __name__ == "__main__":
  sys.exit(main())
