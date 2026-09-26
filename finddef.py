#! /usr/bin/python

import argparse
import shlex
import sys

from git_search import (
  Config,
  build_command,
  has_output_modifiers,
  open_search_result,
  parse_null_results,
  resolve_extensions,
  resolve_setting,
  run_command,
  select_result,
  split_result_idx,
  write_context,
  write_json_results,
)


def _argument_parser():
  parser = argparse.ArgumentParser(
    allow_abbrev=False,
    usage="%(prog)s [OPTIONS] [GIT_GREP_OPTIONS] NAME[/N]",
    description="Find Python definitions using git grep.",
    epilog=(
      "NAME/N selects a result using forgiving legacy rules: 0 and 1 select "
      "the first result, and large values select the last. Use --name and "
      "--select for strict, agent-friendly selection. Git grep options may "
      "also be placed after --."
    ),
  )
  parser.add_argument("--name", help="exact name; disables the /N shorthand")
  parser.add_argument(
    "--select", type=int, metavar="N",
    help="strictly select the one-based result N")
  parser.add_argument(
    "--assignments", action="store_true",
    help="search for assignments instead of classes and functions")
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
  if options.name is not None:
    name = options.name
    result_idx = None
    grep_args = [*remaining, *passthrough]
  else:
    if not remaining or remaining[-1] in ("+debug", "+print", "+t", "+to"):
      parser.error("NAME is required")
    name, result_idx = split_result_idx(remaining[-1])
    grep_args = [*remaining[:-1], *passthrough]

  if options.select is not None and result_idx is not None:
    parser.error("use either NAME/N or --select, not both")
  if options.json and options.open_result:
    parser.error("--json cannot be combined with --open")
  if options.open_result and options.select is None and result_idx is None:
    parser.error("--open requires --select or NAME/N")
  if name == "":
    parser.error("name cannot be empty")

  options.name = name
  options.result_idx = result_idx
  options.grep_args = grep_args
  options.parser = parser
  return options


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
  prefix = options.root or resolve_setting("GG_SEARCH_IN", "./")
  extensions = resolve_extensions(options.ext)
  pathspecs = config.format(prefix, extensions)
  patterns = _definition_patterns(name, assignments)
  structured = (
    options.json
    or options.select is not None
    or options.result_idx is not None
  )
  if structured and has_output_modifiers(config.flags):
    options.parser.error(
      "output-formatting git grep options cannot be combined with "
      "selection or --json")

  cmd = build_command(
    patterns,
    config.flags,
    pathspecs,
    structured=structured,
    untracked=options.untracked,
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
    sys.stdout.buffer.write(completed.stdout)
    return 0

  try:
    results = list(parse_null_results(completed.stdout))
  except ValueError as error:
    sys.stderr.write(f"finddef: {error}\n")
    return 2
  if not results:
    return 1

  if options.select is not None or options.result_idx is not None:
    strict = options.select is not None
    index = options.select if strict else options.result_idx
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
