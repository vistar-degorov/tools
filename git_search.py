from dataclasses import dataclass
from itertools import product
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys


@dataclass(frozen=True)
class SearchResult:
  path: str
  line: int | None
  column: int | None
  text: str


class Config:
  def __init__(self, flags, test_indicators=("[Tt]est",), test_mode="exclude"):
    self.debug_mode = False
    self.hide_filenames = False
    self.only_filenames = False
    self.pats = ("{PREFIX}*{EXTEN}",)
    self.mods = ()
    self.set_test_mode(test_mode, test_indicators)
    self.flags = tuple(self._filter_flags(flags, test_indicators))
    if not self.hide_filenames and not self.only_filenames:
      self.flags += ("-n",)

  def set_test_mode(self, mode, test_indicators):
    if mode == "include":
      self.mods = ()
      self.pats = ("{PREFIX}*{EXTEN}",)
    elif mode == "only":
      self.mods = ()
      self.pats = tuple(
        "{{PREFIX}}*{word}*{{EXTEN}}".format(word=word)
        for word in test_indicators
      )
    else:
      self.mods = tuple(
        ":!:*{word}*".format(word=word)
        for word in test_indicators
      )
      self.pats = ("{PREFIX}*{EXTEN}",)

  def _filter_flags(self, flags, test_indicators):
    for flag in flags:
      if flag in ("+debug", "+print"):
        self.debug_mode = True
      elif flag == "+t":
        self.set_test_mode("include", test_indicators)
      elif flag == "+to":
        self.set_test_mode("only", test_indicators)
      else:
        if flag in ("--files-with-matches", "--name-only"):
          self.only_filenames = True
        elif flag.startswith("-") and not flag.startswith("--"):
          self.hide_filenames = self.hide_filenames or "h" in flag
          self.only_filenames = self.only_filenames or "l" in flag
        yield flag

  def format(self, prefix, extensions):
    pats = (
      pat.format(PREFIX=prefix, EXTEN=ext)
      for pat, ext in product(self.pats, extensions)
    )
    return (*self.mods, *pats)


def split_result_idx(query):
  if match := re.search(r"/(\d+)$", query):
    return query[:match.start()], int(match.group(1))
  return query, None


_GIT_GREP_OPTIONS_WITH_VALUE = {
  "-A", "--after-context",
  "-B", "--before-context",
  "-C", "--context",
  "-e", "--regexp",
  "-f", "--file",
  "-m", "--max-count",
  "--max-depth",
  "--threads",
}


def _split_leading_git_options(arguments, parser):
  index = 0
  patterns = []
  has_pattern_source = False
  patterns_complete = True
  while index < len(arguments):
    argument = arguments[index]
    if argument in ("+debug", "+print", "+t", "+to", "(", ")"):
      index += 1
      continue
    if not argument.startswith("-"):
      break
    index += 1
    if argument in _GIT_GREP_OPTIONS_WITH_VALUE:
      if index == len(arguments):
        parser.error(f"{argument} requires an argument")
      if argument in ("-e", "--regexp"):
        patterns.append(arguments[index])
        has_pattern_source = True
      elif argument in ("-f", "--file"):
        has_pattern_source = True
        patterns_complete = False
      index += 1
    elif argument.startswith("-e") and len(argument) > 2:
      patterns.append(argument[2:])
      has_pattern_source = True
    elif argument.startswith("--regexp="):
      patterns.append(argument.partition("=")[2])
      has_pattern_source = True
    elif argument.startswith("-f") and len(argument) > 2:
      has_pattern_source = True
      patterns_complete = False
    elif argument.startswith("--file="):
      has_pattern_source = True
      patterns_complete = False
  return (
    arguments[:index],
    arguments[index:],
    tuple(patterns),
    has_pattern_source,
    patterns_complete,
  )


def _replace_option(arguments, old_option, new_option):
  replaced = []
  for index, argument in enumerate(arguments):
    if argument == "--":
      replaced.extend(arguments[index:])
      break
    if argument == old_option:
      replaced.append(new_option)
    elif argument.startswith(f"{old_option}="):
      replaced.extend((new_option, argument.partition("=")[2]))
    elif (
      len(old_option) == 2
      and argument.startswith(old_option)
      and len(argument) > 2
    ):
      replaced.extend((new_option, argument[2:]))
    else:
      replaced.append(argument)
  return replaced


def _pop_trailing_result_idx(arguments):
  if arguments and (match := re.fullmatch(r"/(\d+)", arguments[-1])):
    return arguments[:-1], int(match.group(1))
  return arguments, None


def parse_search_arguments(
  parser,
  value_attribute,
  value_label,
  arguments=None,
  *,
  pattern_options=(),
):
  arguments = list(sys.argv[1:] if arguments is None else arguments)
  for pattern_option in pattern_options:
    arguments = _replace_option(arguments, pattern_option, "--regexp")
  if "--" in arguments:
    separator = arguments.index("--")
    wrapper_arguments = arguments[:separator]
    path_arguments = arguments[separator + 1:]
  else:
    wrapper_arguments = arguments
    path_arguments = None

  options, remaining = parser.parse_known_args(wrapper_arguments)
  (
    grep_args,
    operands,
    grep_patterns,
    has_pattern_source,
    patterns_complete,
  ) = _split_leading_git_options(remaining, parser)
  if pattern_options:
    grep_args = _replace_option(grep_args, "--regexp", "-e")
  if path_arguments is None:
    operands, trailing_result_idx = _pop_trailing_result_idx(operands)
  else:
    path_arguments, trailing_result_idx = _pop_trailing_result_idx(
      path_arguments)

  value = getattr(options, value_attribute)
  if pattern_options and has_pattern_source:
    command_patterns = ()
    pattern_texts = grep_patterns
    result_idx = None
    scope_arguments = operands
  elif value is not None:
    command_patterns = (value,)
    pattern_texts = command_patterns
    result_idx = None
    scope_arguments = operands
  else:
    if not operands:
      parser.error(f"{value_label} is required")
    value, result_idx = split_result_idx(operands[0])
    command_patterns = (value,)
    pattern_texts = command_patterns
    scope_arguments = operands[1:]

  if any(argument.startswith("-") for argument in scope_arguments):
    parser.error(f"git grep options must precede {value_label}")
  if result_idx is not None and trailing_result_idx is not None:
    parser.error("result number specified more than once")
  if trailing_result_idx is not None:
    result_idx = trailing_result_idx

  if path_arguments is not None:
    scope_arguments = [*scope_arguments, "--", *path_arguments]
  elif not scope_arguments:
    scope_arguments = None

  if options.show is not None and result_idx is not None:
    parser.error(f"use either /N or --show N, not both")
  if options.json and options.open_result:
    parser.error("--json cannot be combined with --open")
  if options.open_result and options.show is None and result_idx is None:
    parser.error(f"--open requires --show or {value_label}/N")
  if value == "":
    parser.error(f"{value_label.lower()} cannot be empty")
  if scope_arguments is not None and options.root is not None:
    parser.error("--root cannot be combined with revisions or paths")
  if scope_arguments is not None and options.ext is not None:
    parser.error("--ext cannot be combined with revisions or paths")

  setattr(options, value_attribute, value)
  options.result_idx = result_idx
  options.grep_args = grep_args
  options.scope_arguments = scope_arguments
  options.command_patterns = command_patterns
  options.pattern_texts = pattern_texts
  options.patterns_complete = patterns_complete
  options.parser = parser
  return options


def remove_color_codes(text):
  return re.sub(r"\x1b\[[0-9;]*m", "", text)


def resolve_setting(name, fallback):
  if value := os.getenv(name):
    return value
  if value := os.getenv(f"{name}_DEFAULT"):
    return value
  return fallback


def resolve_extensions(values):
  if values:
    extensions = [item for value in values for item in value.split()]
  else:
    extensions = resolve_setting("GG_SEARCH_EXT", ".py").split()
  return tuple(
    extension if extension.startswith(".") else f".{extension}"
    for extension in extensions
  )


def build_command(
  patterns,
  grep_args,
  scope_arguments,
  *,
  structured=False,
  untracked=False,
  word_regexp=False,
):
  cmd = ["git", "grep"]
  if untracked:
    cmd.extend(("--untracked", "--exclude-standard"))
  if structured:
    cmd.extend(("--color=never", "-I", "-n", "--column", "-z"))
  else:
    cmd.append("--color=always" if sys.stdout.isatty() else "--color=never")
  if word_regexp:
    cmd.append("-w")
  cmd.extend(grep_args)
  for pattern in patterns:
    cmd.extend(("-e", pattern))
  cmd.extend(scope_arguments)
  return tuple(cmd)


def run_command(cmd):
  return subprocess.run(cmd, stdout=subprocess.PIPE)


def parse_null_results(output):
  offset = 0
  output_length = len(output)
  while offset < output_length:
    path_end = output.find(b"\0", offset)
    line_end = output.find(b"\0", path_end + 1)
    column_end = output.find(b"\0", line_end + 1)
    text_end = output.find(b"\n", column_end + 1)
    if min(path_end, line_end, column_end, text_end) < 0:
      raise ValueError("unexpected git grep output")

    yield SearchResult(
      path=os.fsdecode(output[offset:path_end]),
      line=int(output[path_end + 1:line_end]),
      column=int(output[line_end + 1:column_end]),
      text=output[column_end + 1:text_end].decode("utf-8", "replace"),
    )
    offset = text_end + 1


def parse_null_paths(output):
  for path in output.split(b"\0"):
    if path:
      yield SearchResult(os.fsdecode(path), None, None, "")


def select_result(results, index, *, clamp):
  if not results:
    return None
  if clamp:
    normalized_index = min(max(index, 1), len(results))
    return results[normalized_index - 1]
  if index < 1 or index > len(results):
    raise IndexError(index)
  return results[index - 1]


def write_json_results(results):
  for result in results:
    print(json.dumps({
      "path": result.path,
      "line": result.line,
      "column": result.column,
      "text": result.text,
    }))


def write_result(result):
  print(f"{result.path}:{result.line}:{result.text}")


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


def has_output_modifiers(arguments):
  long_options = {
    "--after-context", "--before-context", "--break", "--context",
    "--color",
    "--count", "--files-with-matches", "--files-without-match",
    "--function-context", "--heading", "--name-only", "--no-filename",
    "--null", "--show-function",
  }
  for argument in arguments:
    if argument in long_options:
      return True
    if any(argument.startswith(f"{option}=") for option in long_options):
      return True
    if re.match(r"-[ABC]\d*$", argument):
      return True
    if re.match(r"-[cLlhpW]+$", argument):
      return True
  return False
