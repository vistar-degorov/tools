#! /usr/bin/python

import argparse
import shlex
import sys

from git_search_args import (
  Config,
  has_output_modifiers,
  parse_search_arguments,
  resolve_extensions,
  resolve_setting,
)
from git_search import (
  build_command,
  open_search_result,
  parse_null_paths,
  parse_null_results,
  run_command,
  select_result,
  write_context,
  write_json_results,
)


def _argument_parser():
  root_default = resolve_setting("GG_SEARCH_IN", "./")
  extension_default = resolve_setting("GG_SEARCH_EXT", ".py")
  parser = argparse.ArgumentParser(
    allow_abbrev=False,
    usage=(
      "%(prog)s [OPTIONS] [GIT_GREP_OPTIONS] NAME[/N] "
      "[REV_OR_PATH ...] [/N]"),
    description="Find Python definitions using git grep.",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog=(
      "Examples:\n"
      "  finddef NAME\n"
      "  finddef -A 2 NAME\n"
      "  finddef NAME PATH ...\n"
      "  finddef NAME -- PATH ...\n"
      "  finddef NAME PATH ... /N\n"
      "\n"
      "Git grep options go before NAME. Remaining positional arguments are "
      "passed to git grep as revisions or paths; -- before paths is "
      "optional. Explicit operands override the default root and extension "
      "filters.\n"
      "Results include line numbers by default, so -n is unnecessary."
    ),
  )
  parser.add_argument(
    "--name", help="set the name explicitly; useful when it ends in /N")
  parser.add_argument(
    "--show", type=int, metavar="N",
    help="print the Nth search result in context of surrounding text")
  parser.add_argument(
    "--assignments", action="store_true",
    help="search for assignments instead of classes and functions")
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
  parser.add_argument(
    "--debug", action="store_true", help="print the git grep command")
  return parser


def _parse_arguments(arguments=None):
  return parse_search_arguments(
    _argument_parser(), "name", "NAME", arguments)


def _definition_patterns(name, assignments):
  if assignments:
    return (rf"\b{name} \+= *[^=].*",)
  return (
    rf"\bclass \+{name} *:",
    rf"\bclass \+{name} *(.\+) *:",
    rf"\bdef \+{name} *(.*",
    rf"\bdef \+{name} *(.*) *->.\+:",
  )


def main(arguments=None):
  options = _parse_arguments(arguments)
  name = options.name
  assignments = options.assignments
  if name.endswith("="):
    assignments = True
    name = name.rstrip("=")
  if name == "":
    options.parser.error("name cannot be empty")

  config = Config(options.grep_args, test_mode=options.tests or "exclude")
  extensions = resolve_extensions(options.ext)
  if options.scope_arguments is None:
    prefix = options.root or resolve_setting("GG_SEARCH_IN", "./")
    scope_arguments = ("--", *config.format(prefix, extensions))
  else:
    scope_arguments = (*options.scope_arguments, *config.mods)
  patterns = _definition_patterns(name, assignments)
  selecting_result = (
    options.show is not None or options.result_idx is not None)
  structured = (
    options.json
    or selecting_result
  )
  selecting_filename = selecting_result and config.only_filenames
  if structured and has_output_modifiers(config.flags) and not (
    selecting_filename and not options.json
  ):
    options.parser.error(
      "output-formatting git grep options cannot be combined with "
      "selection or --json")

  cmd = build_command(
    patterns,
    config.flags,
    scope_arguments,
    structured=structured,
    untracked=options.untracked,
  )
  if options.debug or config.debug_mode:
    print(shlex.join(cmd))
    return 0

  completed = run_command(cmd)
  if completed.returncode != 0:
    return completed.returncode
  if not structured:
    sys.stdout.buffer.write(completed.stdout)
    return 0

  try:
    if selecting_filename:
      results = list(parse_null_paths(completed.stdout))
    else:
      results = list(parse_null_results(completed.stdout))
  except ValueError as error:
    sys.stderr.write(f"finddef: {error}\n")
    return 2
  if not results:
    return 1

  if options.show is not None or options.result_idx is not None:
    strict = options.show is not None
    index = options.show if strict else options.result_idx
    try:
      result = select_result(results, index, clamp=not strict)
    except IndexError:
      sys.stderr.write(
        f"finddef: result {index} is outside 1..{len(results)}\n")
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

  write_json_results(results)
  return 0


if __name__ == "__main__":
  sys.exit(main())
