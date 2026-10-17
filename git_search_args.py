from enum import Enum
from itertools import chain, product
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
    self.test_indicators = test_indicators
    self.set_test_mode(test_mode)
    self.flags = tuple(_LegacyOptionsConfig(flags, config=self))
    if not self.hide_filenames and not self.only_filenames:
      self.flags += ("-n",)

  def set_test_mode(self, mode):
    if mode == "include":
      self.mods = ()
      self.pats = ("{PREFIX}*{EXTEN}",)
    elif mode == "only":
      self.mods = ()
      self.pats = tuple(
        f"{{PREFIX}}*{word}*{{EXTEN}}" for word in self.test_indicators
      )
    else:
      self.mods = tuple(f":!:*{word}*" for word in self.test_indicators)
      self.pats = ("{PREFIX}*{EXTEN}",)

  def format(self, prefix, extensions):
    pats = (
      pat.format(PREFIX=prefix, EXTEN=ext) for pat, ext in product(self.pats, extensions)
    )
    return (*self.mods, *pats)


def split_result_idx(query):
  if match := re.search(r"/(\d+)$", query):
    return query[:match.start()], int(match.group(1))
  return query, None


class OptionValueStyle(Enum):
  EQUALS = "k=value"
  SPACED = "k value"
  GLUED = "kvalue"
VS = OptionValueStyle


_HIDDEN_PATTERN_OPTION = "--gg-pattern-internal"


GIT_GREP_OPTIONS_WITH_VALUE = {
  "-A": (VS.SPACED, VS.GLUED), "--after-context": (VS.EQUALS, VS.SPACED),
  "-B": (VS.SPACED, VS.GLUED), "--before-context": (VS.EQUALS, VS.SPACED),
  "-C": (VS.SPACED, VS.GLUED), "--context": (VS.EQUALS, VS.SPACED),
  "--color": (VS.EQUALS,),
  "-e": (VS.SPACED, VS.GLUED),
  "-f": (VS.SPACED, VS.GLUED), "--file": (VS.EQUALS, VS.SPACED),
  "-m": (VS.SPACED, VS.GLUED), "--max-count": (VS.EQUALS, VS.SPACED),
  "--max-depth": (VS.EQUALS, VS.SPACED),
  "--threads": (VS.EQUALS, VS.SPACED),
  "-O": (VS.GLUED,),
}

_CONTEXT_SETTINGS = {
  "-A": ("after_context",),
  "--after-context": ("after_context",),
  "-B": ("before_context",),
  "--before-context": ("before_context",),
  "-C": ("before_context", "after_context"),
  "--context": ("before_context", "after_context"),
  "-p": ("show_function",),
  "--show-function": ("show_function",),
  "-W": ("function_context",),
  "--function-context": ("function_context",),
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

_OTHER_OPTIONS = frozenset((
  "--all-match", "--and", "--basic-regexp", "--cached", "--exclude-standard",
  "--extended-regexp", "--fixed-strings", "--full-name", "--ignore-case", "--invert-match",
  "--no-color", "--no-exclude-standard", "--no-index", "--no-recursive", "--not", "--no-textconv",
  "--only-matching", "--open-files-in-pager", "--or", "--parent-basename", "--perl-regexp",
  "--recurse-submodules", "--recursive", "--text", "--textconv", "--untracked", "--word-regexp",
))

_ALL_LONG_OPTIONS = frozenset(name for name in (
  *GIT_GREP_OPTIONS_WITH_VALUE, *_CONTEXT_SETTINGS, *_LONG_BOOLEAN_SETTINGS, *_OTHER_OPTIONS
  ) if name.startswith("--")
)


def _resolve_prefix(prefix):
  if prefix in ("-", "--"):
    return prefix
  opts = [opt for opt in _ALL_LONG_OPTIONS if opt.startswith(prefix)]
  if len(opts) == 1:
    return opts[0]
  return prefix


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


def _filter_short_options(idx, argument, later_arguments, submit):
  if len(argument) < 2:
    yield from submit(argument, idx, argument)

  argument = argument[1:]
  for jdx, key in enumerate(argument):
    key = "-" + key
    value_styles = GIT_GREP_OPTIONS_WITH_VALUE.get(key, ())

    if jdx + 1 == len(argument) and VS.SPACED in value_styles:
      _, value = next(later_arguments, (None, None))
      yield from submit(key, idx, key, value, VS.SPACED)

    elif VS.GLUED in value_styles:
      value = argument[jdx + 1:]
      yield from submit(key + value, idx, key, value, VS.GLUED)
      break

    else:
      yield from submit(key, idx, key)


def _filter_args(arguments, submit):
  arguments = iter(enumerate(arguments))
  for idx, argument in arguments:
    key, equals, value = argument.partition("=")
    key = _resolve_prefix(key)
    value_styles = (
      (VS.SPACED,) if key == _HIDDEN_PATTERN_OPTION
      else GIT_GREP_OPTIONS_WITH_VALUE.get(key, ())
    )

    if equals:
      if VS.EQUALS in value_styles:
        yield from submit(argument, idx, key, value, VS.EQUALS)
      else:
        yield from submit(argument, idx, argument)

    elif VS.SPACED in value_styles:
      _, value = next(arguments, (None, None))
      yield from submit(argument, idx, key, value, VS.SPACED)

    elif argument.startswith("-") and not argument.startswith("--"):
      yield from _filter_short_options(idx, argument, arguments, submit)

    else:
      yield from submit(argument, idx, key)


class OptionsIterator:
  def __init__(self, arguments):
    self.current_idx = 0
    self.current_option = None
    self.current_option_value = None
    arguments = iter(arguments)

    def _arguments_until_dashdash():
      for argument in arguments:
        yield argument
        if argument == "--":
          break

    filtered_args = _filter_args(_arguments_until_dashdash(), self.submit)
    self._output = chain(filtered_args, self.pop_current_options(), arguments)

  def __iter__(self):
    return self

  def __next__(self):
    return next(self._output)

  def append_option(self, argument, value=None, value_style=None):
    if value_style in (VS.EQUALS, VS.GLUED):
      value = None # value is already contained in the argument
    if self.current_option is None:
      self.current_option = argument
    else:
      self.current_option += argument.removeprefix("-")

    self.current_option_value = value

  def pop_current_options(self):
    if self.current_option is not None:
      yield self.current_option
    if self.current_option_value is not None:
      yield self.current_option_value
    self.current_option = None
    self.current_option_value = None

  def submit(self, argument, idx, key, value=None, value_style=None):
    if idx != self.current_idx:
      self.current_idx = idx
      yield from self.pop_current_options()
    self.consume(argument, key, value, value_style)

  def consume(self, argument, key, value=None, value_style=None):
    self.append_option(argument, value, value_style)


class _LegacyOptionsConfig(OptionsIterator):
  filename_only_options = frozenset((
    "-l", "-L",  "--name-only",
    "--files-with-matches", "--files-without-match",
  ))

  def __init__(self, arguments, config):
    super().__init__(arguments)
    self.config = config

  def consume(self, argument, key, value=None, value_style=None):
    if key in ("+debug", "+print"):
      self.config.debug_mode = True
    elif key == "+t":
      self.config.set_test_mode("include")
    elif key == "+to":
      self.config.set_test_mode("only")
    else:
      self.config.hide_filenames |= key == "-h"
      self.config.only_filenames |= key in self.filename_only_options
      self.append_option(argument, value, value_style)


class SkipContextOptions(OptionsIterator):
  def consume(self, argument, key, value=None, value_style=None):
    if key not in _CONTEXT_SETTINGS and not key.removeprefix("-").isdigit():
      self.append_option(argument, value, value_style)


class ApplyOutputOptions(OptionsIterator):
  def __init__(self, arguments, settings):
    self.settings = settings
    super().__init__(arguments)

  def consume(self, argument, key, value=None, value_style=None):
    enabled = True
    if key.startswith("--no-"):
      key = _resolve_prefix("--" + key.removeprefix("--no-"))
      enabled = False

    if context_settings := _CONTEXT_SETTINGS.get(key):
      for setting in context_settings:
        setattr(self.settings, setting, enabled)
    elif setting := _LONG_BOOLEAN_SETTINGS.get(key):
      setattr(self.settings, setting, enabled)
    elif len(key) == 2 and (update := _SHORT_SETTINGS.get(key[1])):
      setting, setting_value = update
      setattr(self.settings, setting, setting_value)
    elif key.startswith("-") and key[1:].isdigit():
      self.settings.before_context = True
      self.settings.after_context = True
    elif key == "--color":
      self.settings.color = (
        value if enabled and value_style == VS.EQUALS
        else "always" if enabled else "never"
      )

    is_presentation_option = (
      key in _PRESENTATION_LONG_OPTIONS
      or len(key) == 2 and key[1] in _PRESENTATION_SHORT_OPTIONS
    )
    valid_presentation_option = is_presentation_option and (
      value_style != VS.EQUALS
      or key == "--color" and enabled and value in _COLOR_VALUES
    )
    if not valid_presentation_option:
      self.append_option(argument, value, value_style)


def without_context_options(arguments):
  return SkipContextOptions(arguments)


class _HasOutputModifiers(OptionsIterator):
  long_options = frozenset((
    "--break", "--color", "--count", "--null", "--heading", "--name-only",
    "--no-filename", "--files-with-matches", "--files-without-match",
    *_CONTEXT_SETTINGS,
  ))
  def __init__(self, arguments):
    super().__init__(arguments)
    self.has_output_modifiers = False

  def consume(self, argument, key, value=None, value_style=None):
    self.has_output_modifiers |= (key in self.long_options
      or len(key) == 2 and key[1] in "ABCcLlhpW"
      or key.startswith("-") and key[1:].isdigit())
    self.append_option(argument, value, value_style)


def has_output_modifiers(arguments):
  for _ in (arguments := _HasOutputModifiers(arguments)):
    if arguments.has_output_modifiers:
      return True
  return False


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
  arguments = tuple(ApplyOutputOptions(arguments, settings))

  if settings.files_with_matches or settings.files_without_match:
    mode = GrepOutputMode.PATHS
    settings.show_filename = True
  elif settings.count:
    mode = GrepOutputMode.COUNTS
  elif any((
    settings.before_context,
    settings.after_context,
    settings.show_function,
    settings.function_context,
  )):
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


class _LeadingGitOptions(OptionsIterator):
  non_option_arguments = frozenset(("+debug", "+print", "+t", "+to", "(", ")"))

  def __init__(self, arguments, parser):
    self.parser = parser
    self.leading = True
    self.patterns = []
    self.has_pattern_source = False
    self.all_patterns_known = True
    super().__init__(arguments)

  def consume(self, argument, key, value=None, value_style=None):
    if not self.leading:
      return
    if not argument.startswith("-") and argument not in self.non_option_arguments:
      self.leading = False
      return
    if value_style == VS.SPACED and value is None:
      self.parser.error(f"{argument} requires an argument")

    if key in ("-e", _HIDDEN_PATTERN_OPTION):
      self.patterns.append(value)
      self.has_pattern_source = True
    elif key in ("-f", "--file"):
      self.has_pattern_source = True
      self.all_patterns_known = False
    if key == _HIDDEN_PATTERN_OPTION:
      argument = "-e"
    self.append_option(argument, value, value_style)


def _split_leading_git_options(arguments, parser):
  options = _LeadingGitOptions(arguments, parser)
  leading_arguments = list(options)
  return (
    leading_arguments,
    arguments[len(leading_arguments):],
    tuple(options.patterns),
    options.has_pattern_source,
    options.all_patterns_known,
  )


def _mask_pattern_options(arguments, pattern_options):
  masked = []
  pattern_options = frozenset(pattern_options)
  for index, argument in enumerate(arguments):
    if argument == "--":
      masked.extend(arguments[index:])
      break

    key, equals, value = argument.partition("=")
    if key in pattern_options:
      masked.append(_HIDDEN_PATTERN_OPTION)
      if equals:
        masked.append(value)
      continue

    if any(
      len(option) == 2 and argument.startswith(option) and len(argument) > 2
      for option in pattern_options
    ):
      masked.extend((_HIDDEN_PATTERN_OPTION, argument[2:]))
    else:
      masked.append(argument)
  return masked


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
  arguments = _mask_pattern_options(arguments, pattern_options)
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
