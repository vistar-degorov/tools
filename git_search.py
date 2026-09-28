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
  line: int
  column: int
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
        if flag.startswith("-") and not flag.startswith("--"):
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


def parse_search_arguments(
  parser,
  value_attribute,
  value_label,
  arguments=None,
):
  arguments = list(sys.argv[1:] if arguments is None else arguments)
  if "--" in arguments:
    separator = arguments.index("--")
    wrapper_arguments = arguments[:separator]
    pathspecs = arguments[separator + 1:]
  else:
    wrapper_arguments = arguments
    pathspecs = None

  options, remaining = parser.parse_known_args(wrapper_arguments)
  value = getattr(options, value_attribute)
  if value is not None:
    result_idx = None
    grep_args = remaining
  else:
    if not remaining or remaining[-1] in (
      "+debug", "+print", "+t", "+to",
    ):
      parser.error(f"{value_label} is required")
    value, result_idx = split_result_idx(remaining[-1])
    grep_args = remaining[:-1]

  if options.select is not None and result_idx is not None:
    parser.error(f"use either {value_label}/N or --select, not both")
  if options.json and options.open_result:
    parser.error("--json cannot be combined with --open")
  if options.open_result and options.select is None and result_idx is None:
    parser.error(f"--open requires --select or {value_label}/N")
  if value == "":
    parser.error(f"{value_label.lower()} cannot be empty")
  if pathspecs is not None and options.root is not None:
    parser.error("--root cannot be combined with paths after --")
  if pathspecs is not None and options.ext is not None:
    parser.error("--ext cannot be combined with paths after --")

  setattr(options, value_attribute, value)
  options.result_idx = result_idx
  options.grep_args = grep_args
  options.pathspecs = pathspecs
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
  pathspecs,
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
  cmd.extend(("--", *pathspecs))
  return tuple(cmd)


def run_command(cmd):
  return subprocess.run(cmd, stdout=subprocess.PIPE)


def write_pathspec_hint(command_name, grep_args):
  if any(not argument.startswith(("-", "+")) for argument in grep_args):
    sys.stderr.write(
      f"{command_name}: if any arguments are paths, put them after `--`\n")


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
  if executable in ("vim", "nvim"):
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
