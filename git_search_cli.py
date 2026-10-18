from dataclasses import dataclass
import shlex
import subprocess
import sys

from git_search import (
  SearchResult,
  GrepOutputMode,
  build_command,
  open_search_result,
  output_uses_color,
  parse_grep_results,
  select_result,
  write_context,
  write_json_results,
  write_records,
)
from git_search_args import Config, has_output_modifiers, resolve_setting, without_context_options


@dataclass(frozen=True)
class SearchScope:
  grep_args: tuple[str, ...]
  arguments: tuple[str, ...]
  debug: bool


@dataclass(frozen=True)
class ResultAction:
  index: int | None
  clamp_index: bool
  json: bool
  open_result: bool
  stdout: bool
  tool: str | None
  filename_only: bool

  @property
  def machine_output(self):
    return self.json or self.index is not None


def prepare_search_scope(options, grep_args, extensions, *, test_indicators=("[Tt]est",)):
  config = Config(
    grep_args, test_indicators=test_indicators, test_mode=options.tests or "exclude"
  )
  if options.scope_arguments is None:
    prefix = options.root or resolve_setting("GG_SEARCH_IN", "./")
    arguments = ("--", *config.format(prefix, extensions))
  else:
    arguments = (*options.scope_arguments, *config.mods)
  return SearchScope(config.flags, arguments, config.debug_mode)


def make_result_action(options, output_plan):
  selecting_result = options.show is not None or options.result_idx is not None
  filename_only = selecting_result and output_plan.mode is GrepOutputMode.PATHS
  if (
    has_output_modifiers(options.grep_args)
    and (options.json or selecting_result)
    and not (filename_only and not options.json)
  ):
    options.parser.error(
      "output-formatting git grep options cannot be combined with selection or --json"
    )

  # --show N will error when N is OOB, and /N will clamp
  clamp_index = options.show is None
  return ResultAction(
    index=options.result_idx if clamp_index else options.show,
    clamp_index=clamp_index,
    json=options.json,
    open_result=options.open_result,
    stdout=options.stdout,
    tool=options.tool,
    filename_only=filename_only,
  )


def build_search_command(
  patterns,
  grep_args,
  scope_arguments,
  output_plan,
  result_action,
  *,
  untracked=False,
  word_regexp=False,
  stream=None,
):
  if stream is None:
    stream = sys.stdout
  return build_command(
    patterns,
    grep_args,
    scope_arguments,
    color=not result_action.machine_output and output_uses_color(output_plan, stream),
    untracked=untracked,
    word_regexp=word_regexp,
  )


def build_occurrence_search_command(
  patterns,
  grep_args,
  scope_arguments,
  *,
  untracked=False,
  word_regexp=False,
):
  return build_command(
    patterns,
    (*without_context_options(grep_args), "-o"),
    scope_arguments,
    untracked=untracked,
    word_regexp=word_regexp,
  )


def execute_search(command, output_plan, *, program, debug=False):
  if debug:
    print(shlex.join(command))
    return 0, None

  completed = subprocess.run(command, stdout=subprocess.PIPE)
  if completed.returncode != 0:
    return completed.returncode, None
  if output_plan.quiet:
    return 0, None

  try:
    return 0, list(parse_grep_results(completed.stdout, output_plan.mode))
  except ValueError as error:
    sys.stderr.write(f"{program}: {error}\n")
    return 2, None


def present_results(results, output_plan, result_action, *, program):
  if not results:
    return 1

  if result_action.filename_only:
    results = [SearchResult(result.path, None, None, b"") for result in results]

  if result_action.index is not None:
    try:
      result = select_result(results, result_action.index, clamp=result_action.clamp_index)
    except IndexError:
      sys.stderr.write(
        f"{program}: result {result_action.index} is outside 1..{len(results)}\n"
      )
      return 2

    if result_action.json:
      write_json_results((result,))
      return 0

    should_open = result_action.open_result or (
      result_action.clamp_index and not result_action.stdout and sys.stdout.isatty()
    )
    if should_open:
      tool = result_action.tool
      if tool is None:
        tool = resolve_setting("GG_TOOL", "")
      return open_search_result(tool, result)
    return write_context(result)

  if result_action.json:
    write_json_results(results)
  else:
    write_records(results, output_plan, sys.stdout.buffer)
  return 0
