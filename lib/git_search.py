from dataclasses import dataclass, replace
import json
import os
from itertools import chain
from pathlib import Path
import re
import shlex
import subprocess
import sys

from .git_search_args import (
  Config, GrepOutputMode, prepare_occurrence_options, resolve_setting,
)


@dataclass(frozen=True)
class SearchResult:
  path: str
  line: int | None
  column: int | None
  text: bytes
  # zero-based, half-open byte ranges in text with ANSI color codes removed
  occurrence_ranges: tuple[tuple[int, int], ...] = ()

  @property
  def is_match(self):
    '''
    distinguish grep-matched lines from surrounding context lines
    '''
    return self.line is not None and self.column is not None


@dataclass(frozen=True)
class ContextBlock:
  path: str
  lines: tuple[SearchResult, ...]

  @property
  def matches(self):
    return tuple(line for line in self.lines if line.is_match)


@dataclass(frozen=True)
class PathResult:
  path: str


@dataclass(frozen=True)
class CountResult:
  path: str
  count: int


_SEPARATOR_COLOR = b"\x1b[36m"
_COLOR_RESET = b"\x1b[m"
_COLOR_CODE = re.compile(br"\x1b\[[0-9;]*m")


def remove_color_codes(text):
  if isinstance(text, bytes):
    return _COLOR_CODE.sub(b"", text)
  return re.sub(r"\x1b\[[0-9;]*m", "", text)


def output_uses_color(plan, stream):
  return (
    plan.color == "always"
    or plan.color in (None, "auto") and stream.isatty()
  )


def build_command(
  patterns,
  grep_args,
  scope_arguments,
  *,
  color=False,
  untracked=False,
  word_regexp=False,
):
  cmd = ["git", "grep"]
  if untracked:
    cmd.extend(("--untracked", "--exclude-standard"))
  color = "always" if color else "never"
  cmd.extend((f"--color={color}", "-I", "-H", "-n", "--column", "-z"))
  if word_regexp:
    cmd.append("-w")
  cmd.extend(grep_args)
  for pattern in patterns:
    cmd.extend(("-e", pattern))
  cmd.extend(scope_arguments)
  return tuple(cmd)


def _output_error(offset, message):
  return ValueError(
    f"unexpected git grep output at byte {offset}: {message}")


def _parse_line_records(data, with_context):
  record_rx = re.compile(br"([^\0]+)\0([^\0]+)\0([^\n]*)\n")
  ctx_separator_rx = re.compile(br"(\x1b\[[0-9;]*m)?--(\x1b\[[0-9;]*m)?\n")
  offset = 0

  while True:
    if with_context and (separator := ctx_separator_rx.match(data, offset)):
      yield None, offset
      offset = separator.end()
      continue

    if (record := record_rx.match(data, offset)) is None:
      if offset < len(data):
        raise _output_error(offset, "invalid line record")
      break

    path, line_number, rest = record.group(1, 2, 3)
    try:
      line_number = int(line_number)
    except ValueError:
      raise _output_error(record.start(2), "invalid line number")

    column_end = rest.find(b"\0")
    if column_end < 0:
      if not with_context:
        raise _output_error(record.start(3), "missing column")
      column = None
    else:
      try:
        column = int(rest[:column_end])
      except ValueError:
        raise _output_error(record.start(3), "invalid column number")
      rest = rest[column_end + 1:]

    yield SearchResult(
      path=os.fsdecode(path),
      line=line_number,
      column=column,
      text=rest,
    ), offset
    offset = record.end()


def _make_context_block(lines):
  if not any(line.is_match for line in lines):
    raise ValueError("context contains no matches")
  return ContextBlock(lines[0].path, tuple(lines))


def _parse_context_blocks(data):
  lines = []
  separator_offset = None

  for result, offset in _parse_line_records(data, with_context=True):
    if result is None:
      if not lines:
        raise _output_error(offset, "empty context block")
      yield _make_context_block(lines)
      lines = []
      separator_offset = offset
    else:
      if lines and result.path != lines[0].path:
        yield _make_context_block(lines)
        lines = []
      lines.append(result)
      separator_offset = None

  if lines:
    yield _make_context_block(lines)
  elif separator_offset is not None:
    raise _output_error(separator_offset, "empty context block")


def _parse_path_results(data):
  record_rx = re.compile(br"([^\0]+)\0")
  offset = 0
  while (record := record_rx.match(data, offset)) is not None:
    yield PathResult(os.fsdecode(record.group(1)))
    offset = record.end()
  if offset < len(data):
    raise _output_error(offset, "invalid path record")


def _parse_count_results(data):
  record_rx = re.compile(br"([^\0]+)\0([^\n]*)\n")
  offset = 0
  while (record := record_rx.match(data, offset)) is not None:
    path, count = record.group(1, 2)
    try:
      count = int(count)
    except ValueError:
      raise _output_error(record.start(2), "invalid count")
    yield CountResult(os.fsdecode(path), count)
    offset = record.end()
  if offset < len(data):
    raise _output_error(offset, "invalid count record")


def parse_grep_results(data, mode):
  if mode is GrepOutputMode.MATCHES:
    for result, _ in _parse_line_records(data, with_context=False):
      yield result
  elif mode is GrepOutputMode.CONTEXT:
    yield from _parse_context_blocks(data)
  elif mode is GrepOutputMode.PATHS:
    yield from _parse_path_results(data)
  elif mode is GrepOutputMode.COUNTS:
    yield from _parse_count_results(data)
  else:
    raise ValueError(f"unknown grep output mode: {mode!r}")


def _colored_separator(separator, use_color):
  return _SEPARATOR_COLOR + separator + _COLOR_RESET if use_color else separator


def _raw_text_index(text, visible_index, *, after_color_codes=False):
  raw_index = 0
  current_visible_index = 0
  while current_visible_index < visible_index:
    if color_code := _COLOR_CODE.match(text, raw_index):
      raw_index = color_code.end()
    else:
      raw_index += 1
      current_visible_index += 1
  if after_color_codes:
    while color_code := _COLOR_CODE.match(text, raw_index):
      raw_index = color_code.end()
  return raw_index


def _active_color(text):
  active_color = b""
  for color_code in _COLOR_CODE.finditer(text):
    if color_code.group() in (b"\x1b[m", b"\x1b[0m"):
      active_color = b""
    else:
      active_color += color_code.group()
  return active_color


def _to_occurrences(result):
  for head, tail in result.occurrence_ranges:
    raw_head = _raw_text_index(result.text, head)
    raw_tail = _raw_text_index(result.text, tail, after_color_codes=True)
    text = result.text[raw_head:raw_tail]
    if active_color := _active_color(result.text[:raw_head]):
      if _COLOR_CODE.match(text) is None:
        text = active_color + text
    if _active_color(text):
      text += _COLOR_RESET
    yield replace(result, column=head + 1, text=text, occurrence_ranges=())


def _project_occurrences(records, mode):
  if mode is GrepOutputMode.MATCHES:
    for result in records:
      yield from _to_occurrences(result)
  elif mode is GrepOutputMode.CONTEXT:
    for block in records:
      lines = []
      has_occurrences = False
      for result in block.lines:
        if result.occurrence_ranges:
          lines.extend(_to_occurrences(result))
          has_occurrences = True
        elif result.is_match:
          lines.append(replace(result, column=None))
        else:
          lines.append(result)
      if has_occurrences:
        yield replace(block, lines=tuple(lines))
  else:
    yield from records


def _write_line_result(stream, result, plan, *, show_filename, use_color):
  if plan.null_terminate_filenames:
    separator = b"\0"
  else:
    separator = _colored_separator(b":" if result.is_match else b"-", use_color)

  fields = []
  if show_filename:
    fields.append(os.fsencode(result.path))
  if plan.show_line_number:
    fields.append(str(result.line).encode())
  if plan.show_column_number and result.column is not None:
    fields.append(str(result.column).encode())
  fields.append(result.text)
  stream.write(separator.join(fields) + b"\n")


def _write_match_results(records, plan, stream, use_color):
  previous_path = None
  show_heading = plan.heading and plan.show_filename

  for result in records:
    changed_file = previous_path is not None and result.path != previous_path
    if changed_file and plan.break_between_files:
      stream.write(b"\n")
    if show_heading and result.path != previous_path:
      stream.write(os.fsencode(result.path) + b"\n")
    _write_line_result(
      stream, result, plan,
      show_filename=plan.show_filename and not show_heading,
      use_color=use_color,
    )
    previous_path = result.path


def _write_context_blocks(records, plan, stream, use_color):
  previous_path = None
  show_heading = plan.heading and plan.show_filename

  for block in records:
    if previous_path is not None:
      if block.path != previous_path and plan.break_between_files:
        stream.write(b"\n")
      else:
        stream.write(_colored_separator(b"--", use_color) + b"\n")
    if show_heading and block.path != previous_path:
      stream.write(os.fsencode(block.path) + b"\n")
    for result in block.lines:
      _write_line_result(
        stream, result, plan,
        show_filename=plan.show_filename and not show_heading,
        use_color=use_color,
      )
    previous_path = block.path


def _write_path_results(records, plan, stream, use_color):
  terminator = b"\0" if plan.null_terminate_filenames else b"\n"
  for result in records:
    stream.write(os.fsencode(result.path) + terminator)


def _write_count_results(records, plan, stream, use_color):
  separator = b"\0" if plan.null_terminate_filenames else _colored_separator(b":", use_color)
  for result in records:
    if plan.show_filename:
      stream.write(os.fsencode(result.path) + separator)
    stream.write(str(result.count).encode() + b"\n")


def write_records(records, plan, stream):
  if plan.crop_matches:
    records = _project_occurrences(records, plan.mode)
  writers = {
    GrepOutputMode.MATCHES: _write_match_results,
    GrepOutputMode.CONTEXT: _write_context_blocks,
    GrepOutputMode.PATHS: _write_path_results,
    GrepOutputMode.COUNTS: _write_count_results,
  }
  writers[plan.mode](records, plan, stream, output_uses_color(plan, stream))


def _not_empty(items):
  try:
    items[0]
    return items
  except IndexError:
    return None
  except TypeError:
    for item in items:
      return chain((item,), items)
    return None


def select_result(results, index, *, clamp):
  '''index is 1-based'''
  if index < 1:
    if not clamp:
      raise IndexError(index)
    index = 1
  try:
    result_count = len(results)
    if result_count == 0:
      return None
    elif index > result_count:
      if not clamp:
        raise IndexError(index)
      index = result_count
    return results[index - 1]
  except TypeError:
    _idx = index
    result = None
    for result in results:
      _idx -= 1
      if _idx == 0:
        return result
    if _idx > 0 and not clamp:
      raise IndexError(index)
    return result


def write_json_results(results):
  for result in results:
    print(json.dumps({
      "path": result.path,
      "line": result.line,
      "column": result.column,
      "text": result.text.decode("utf-8", "replace"),
    }))


def write_context(result, context=12):
  if result.line is None:
    try:
      sys.stdout.buffer.write(Path(result.path).read_bytes())
    except OSError as error:
      sys.stderr.write(f"Error reading {result.path}: {error}\n")
      return 2
    return 0

  try:
    lines = Path(result.path).read_text(errors="replace").splitlines()
  except OSError as error:
    sys.stderr.write(f"Error reading {result.path}: {error}\n")
    return 2

  first = max(1, result.line - context)
  last = min(len(lines), result.line + context)
  print(f"{result.path}:{result.line}:{result.column}")
  for line_number in range(first, last + 1):
    marker = ">" if line_number == result.line else " "
    print(f"{marker}{line_number:6} | {lines[line_number - 1]}")
  return 0


def open_search_result(tool, result):
  if not tool:
    return write_context(result)

  command = shlex.split(tool)
  if not command:
    return write_context(result)
  executable = Path(command[0]).name
  if result.line is None:
    command.append(result.path)
  elif executable in ("vim", "nvim"):
    command.extend((result.path, f"+{result.line}"))
  else:
    command.append(f"{result.path}:{result.line}")
  return subprocess.run(command).returncode


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

  @property
  def machine_output(self):
    return self.json or self.index is not None


def make_search_scope(options, grep_args, extensions, *, test_indicators=("[Tt]est",)):
  config = Config(
    grep_args, test_indicators=test_indicators, test_mode=options.tests or "exclude"
  )
  if options.scope_arguments is None:
    prefix = options.root or resolve_setting("GG_SEARCH_IN", "./")
    arguments = ("--", *config.format(prefix, extensions))
  else:
    arguments = (*options.scope_arguments, *config.mods)
  return SearchScope(config.flags, arguments, config.debug_mode)


def make_result_action(options):
  # --show N will error when N is OOB, and /N will clamp
  clamp_index = options.show is None
  return ResultAction(
    index=options.result_idx if clamp_index else options.show,
    clamp_index=clamp_index,
    json=options.json,
    open_result=options.open_result,
    stdout=options.stdout,
    tool=options.tool,
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
    prepare_occurrence_options(grep_args),
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
  if output_plan.crop_matches:
    results = _project_occurrences(results, output_plan.mode)
    output_plan = replace(output_plan, crop_matches=False)
  if (results := _not_empty(results)) is None:
    return 1

  filename_only = result_action.index is not None and output_plan.mode is GrepOutputMode.PATHS
  if filename_only:
    results = (SearchResult(result.path, None, None, b"") for result in results)

  if result_action.index is not None:
    try:
      result = select_result(results, result_action.index, clamp=result_action.clamp_index)
      if result is None:
        return 1
    except IndexError:
      sys.stderr.write(f"{program}: result {result_action.index} is outside the result range\n")
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
