from itertools import product
import os
import re
import sys
from types import SimpleNamespace

from git_search import OutputPlan, GrepOutputMode


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
        "{{PREFIX}}*{word}*{{EXTEN}}".format(word=word) for word in test_indicators
      )
    else:
      self.mods = tuple(":!:*{word}*".format(word=word) for word in test_indicators)
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
      pat.format(PREFIX=prefix, EXTEN=ext) for pat, ext in product(self.pats, extensions)
    )
    return (*self.mods, *pats)


def split_result_idx(query):
  if match := re.search(r"/(\d+)$", query):
    return query[:match.start()], int(match.group(1))
  return query, None


GIT_GREP_OPTIONS_WITH_VALUE = {
  "-A", "--after-context",
  "-B", "--before-context",
  "-C", "--context",
  "-e", "--regexp",
  "-f", "--file",
  "-m", "--max-count",
  "--max-depth",
  "--threads",
}


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


def _without_short_context_options(argument):
  short_options = argument[1:]
  if not short_options:
    return argument, False
  if short_options.isdigit():
    return None, False

  remaining = ""
  for index, short_option in enumerate(short_options):
    if short_option in "efmO":
      remaining += short_options[index:]
      return "-" + remaining, short_option != "O" and index == len(short_options) - 1
    if short_option in "ABC":
      return ("-" + remaining if remaining else None), index == len(short_options) - 1
    if short_option not in "pW":
      remaining += short_option

  return ("-" + remaining if remaining else None), False


def without_context_options(arguments):
  arguments = iter(arguments)
  for argument in arguments:
    if argument == "--":
      yield argument
      yield from arguments
      return

    if argument in GIT_GREP_OPTIONS_WITH_VALUE:
      value = next(arguments, None)
      if argument not in _CONTEXT_SETTINGS:
        yield argument
        if value is not None:
          yield value
      continue

    option, assignment, _ = argument.partition("=")
    if assignment and option in _CONTEXT_SETTINGS:
      continue
    if argument in ("--show-function", "--function-context"):
      continue

    if argument.startswith("-") and not argument.startswith("--"):
      argument, skip_next = _without_short_context_options(argument)
      if skip_next:
        next(arguments, None)

    if argument is not None:
      yield argument


def _filter_short_output_option(argument, settings):
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


def _filter_output_arguments(arguments, settings):
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
        assigned_value if enabled and assignment else "always" if enabled else "never"
      )
    elif argument.startswith("-") and not argument.startswith("--"):
      argument = _filter_short_output_option(argument, settings)

    valid_presentation_option = option in _PRESENTATION_LONG_OPTIONS and (
      not assignment
      or option == "--color"
      and enabled
      and assigned_value in _COLOR_VALUES
    )
    if argument is not None and not valid_presentation_option:
      yield argument
      if option_value is not None:
        yield option_value


def prepare_output(arguments):
  settings = SimpleNamespace(
    show_filename=True,
    line_numbers=None,
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
  arguments = tuple(_filter_output_arguments(arguments, settings))

  if settings.files_with_matches or settings.files_without_match:
    mode = GrepOutputMode.PATHS
    settings.show_filename = True
  elif settings.count:
    mode = GrepOutputMode.COUNTS
  elif any(
    (
      settings.before_context,
      settings.after_context,
      settings.show_function,
      settings.function_context,
    )
  ):
    mode = GrepOutputMode.CONTEXT
  else:
    mode = GrepOutputMode.MATCHES

  if mode in (GrepOutputMode.PATHS, GrepOutputMode.COUNTS):
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


def make_output_plan(arguments):
  return prepare_output(arguments)[0]


def _split_leading_git_options(arguments, parser):
  index = 0
  patterns = []
  has_pattern_source = False
  all_patterns_known = True
  while index < len(arguments):
    argument = arguments[index]
    if argument in ("+debug", "+print", "+t", "+to", "(", ")"):
      index += 1
      continue
    if not argument.startswith("-"):
      break
    index += 1
    if argument in GIT_GREP_OPTIONS_WITH_VALUE:
      if index == len(arguments):
        parser.error(f"{argument} requires an argument")
      if argument in ("-e", "--regexp"):
        patterns.append(arguments[index])
        has_pattern_source = True
      elif argument in ("-f", "--file"):
        has_pattern_source = True
        all_patterns_known = False
      index += 1
    elif argument.startswith("-e") and len(argument) > 2:
      patterns.append(argument[2:])
      has_pattern_source = True
    elif argument.startswith("--regexp="):
      patterns.append(argument.partition("=")[2])
      has_pattern_source = True
    elif argument.startswith("-f") and len(argument) > 2:
      has_pattern_source = True
      all_patterns_known = False
    elif argument.startswith("--file="):
      has_pattern_source = True
      all_patterns_known = False
  return (
    arguments[:index],
    arguments[index:],
    tuple(patterns),
    has_pattern_source,
    all_patterns_known,
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
    elif len(old_option) == 2 and argument.startswith(old_option) and len(argument) > 2:
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
    all_patterns_known,
  ) = _split_leading_git_options(remaining, parser)
  if pattern_options:
    grep_args = _replace_option(grep_args, "--regexp", "-e")
  if path_arguments is None:
    operands, trailing_result_idx = _pop_trailing_result_idx(operands)
  else:
    path_arguments, trailing_result_idx = _pop_trailing_result_idx(path_arguments)

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
  options.all_patterns_known = all_patterns_known
  options.parser = parser
  return options


def add_common_search_arguments(parser):
  root_default = resolve_setting("GG_SEARCH_IN", "./")
  extension_default = resolve_setting("GG_SEARCH_EXT", ".py")
  parser.add_argument("--show", type=int, metavar="N",
    help="print the Nth search result in context of surrounding text")
  parser.add_argument("--tests", choices=("exclude", "include", "only"), default=None,
    help="control whether test files are searched (default: exclude)")
  test_shortcuts = parser.add_mutually_exclusive_group()
  test_shortcuts.add_argument("-t", "--include-tests",
    dest="tests", action="store_const", const="include", help="include test files")
  test_shortcuts.add_argument("-T", "--tests-only",
    dest="tests", action="store_const", const="only", help="search only test files")
  parser.add_argument("-u", "--untracked",
    action="store_true", help="include untracked files while respecting .gitignore")
  parser.add_argument("--root", metavar="PATH",
    help=f"search path prefix (effective default: {root_default})")
  parser.add_argument("--ext", action="append", metavar="EXT",
    help=f"file extension to search; may be repeated (effective default: {extension_default})")
  parser.add_argument("--json", action="store_true",
    help="write JSON Lines results")
  output = parser.add_mutually_exclusive_group()
  output.add_argument("--stdout", action="store_true",
    help="print a selected result instead of opening an editor")
  output.add_argument("--open", dest="open_result", action="store_true",
    help="open a selected result even when stdout is not a terminal")
  parser.add_argument("--tool", help="editor command used with --open")
  parser.add_argument("--debug", action="store_true", help="print the git grep command")


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
