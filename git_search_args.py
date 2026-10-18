from dataclasses import asdict
from dataclasses import dataclass
from enum import Enum
from itertools import chain, product
import os
import re
import sys
from types import SimpleNamespace


class GrepOutputMode(Enum):
  MATCHES = "matches"
  CONTEXT = "context"
  PATHS = "paths"
  COUNTS = "counts"


@dataclass(frozen=True)
class OutputPlan:
  mode: GrepOutputMode
  show_filename: bool
  show_line_number: bool
  show_column_number: bool
  heading: bool
  break_between_files: bool
  null_terminate_filenames: bool
  color: str | None
  quiet: bool = False

  def with_mode(self, mode: GrepOutputMode) -> "OutputPlan":
    return OutputPlan(**dict(asdict(self), mode=mode))


class Config:
  def __init__(self, flags, test_indicators=("[Tt]est",), test_mode="exclude"):
    self.debug_mode = False
    self.pats = ("{PREFIX}*{EXTEN}",)
    self.mods = ()
    self.test_indicators = test_indicators
    self.set_test_mode(test_mode)
    self.flags = tuple(_LegacyOptionsConfig(flags, config=self))

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


_MASKED_PATTERN_OPTION = "--gg-pattern-internal"
_MASKED_PATTERN_OPTION_VALUE = {_MASKED_PATTERN_OPTION: (VS.SPACED,)}


GIT_GREP_OPTIONS_WITH_VALUE = {
  "-A": (VS.SPACED, VS.GLUED), "--after-context": (VS.EQUALS, VS.SPACED),
  "-B": (VS.SPACED, VS.GLUED), "--before-context": (VS.EQUALS, VS.SPACED),
  "-C": (VS.SPACED, VS.GLUED), "--context": (VS.EQUALS, VS.SPACED),
  "-e": (VS.SPACED, VS.GLUED), "--pattern": (VS.EQUALS, VS.SPACED),
  "-f": (VS.SPACED, VS.GLUED), "--file": (VS.EQUALS, VS.SPACED),
  "-m": (VS.SPACED, VS.GLUED), "--max-count": (VS.EQUALS, VS.SPACED),
  "-O": (VS.GLUED,),
  "--max-depth": (VS.EQUALS, VS.SPACED),
  "--threads": (VS.EQUALS, VS.SPACED),
  "--open-files-in-pager": (VS.EQUALS,),
  "--color": (VS.EQUALS,),
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
  "--only-matching", "--or", "--parent-basename", "--perl-regexp",
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


def _iter_short_options(idx, argument, later_arguments, submit):
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


def _iter_args(arguments, submit):
  arguments = iter(enumerate(arguments))
  for idx, argument in arguments:
    key, equals, value = argument.partition("=")
    key = _resolve_prefix(key)
    value_styles = (
      GIT_GREP_OPTIONS_WITH_VALUE.get(key, ())
      or _MASKED_PATTERN_OPTION_VALUE.get(key, ())
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
      yield from _iter_short_options(idx, argument, arguments, submit)

    else:
      yield from submit(argument, idx, key)


class ArgumentIterator:
  def __init__(self, arguments):
    self._idx = 0
    self._text = None
    self._value = None
    arguments = iter(arguments)

    def _arguments_until_dashdash():
      for argument in arguments:
        yield argument
        if argument == "--":
          break

    filtered_args = _iter_args(_arguments_until_dashdash(), self._submit)
    self._output = chain(filtered_args, self._pop_current(), arguments)

  def __iter__(self):
    return self

  def __next__(self):
    return next(self._output)

  def _submit(self, text, idx, key, value=None, value_style=None):
    if idx != self._idx:
      self._idx = idx
      yield from self._pop_current()
    self.inspect(text, key, value, value_style)

  def _pop_current(self):
    if self._text is not None:
      yield self._text
    if self._value is not None:
      yield self._value
    self._text = None
    self._value = None

  def retain(self, text, value=None, value_style=None):
    if value_style in (VS.EQUALS, VS.GLUED):
      value = None # value is already contained in the text
    if self._text is None:
      self._text = text
    else:
      self._text += text.removeprefix("-")
    self._value = value

  def inspect(self, text, key, value=None, value_style=None):
    self.retain(text, value, value_style)


class _LegacyOptionsConfig(ArgumentIterator):
  def __init__(self, arguments, config):
    super().__init__(arguments)
    self.config = config

  def inspect(self, text, key, value=None, value_style=None):
    if key in ("+debug", "+print"):
      self.config.debug_mode = True
    elif key == "+t":
      self.config.set_test_mode("include")
    elif key == "+to":
      self.config.set_test_mode("only")
    else:
      self.retain(text, value, value_style)


class _SkipContextOptions(ArgumentIterator):
  def inspect(self, text, key, value=None, value_style=None):
    if key not in _CONTEXT_SETTINGS and not key.removeprefix("-").isdigit():
      self.retain(text, value, value_style)


_PATH_OUTPUT_OPTIONS = frozenset((
  "-l", "-L", "--name-only", "--files-with-matches", "--files-without-match",
))

_INCOMPATIBLE_STRUCTURED_OUTPUT_OPTIONS = frozenset((
  "-c", "--count",
  "-q", "--quiet",
  "-o", "--only-matching",
  "-O", "--open-files-in-pager",
  *_CONTEXT_SETTINGS,
))


class _SkipIncompatibleOutputOptions(ArgumentIterator):
  def __init__(self, arguments, keep_paths):
    self.keep_paths = keep_paths
    super().__init__(arguments)

  def inspect(self, text, key, value=None, value_style=None):
    incompatible = (
      key in _INCOMPATIBLE_STRUCTURED_OUTPUT_OPTIONS
      or key.startswith("-") and key[1:].isdigit()
      or not self.keep_paths and key in _PATH_OUTPUT_OPTIONS
    )
    if not incompatible:
      self.retain(text, value, value_style)


class _ApplyOutputOptions(ArgumentIterator):
  def __init__(self, arguments, settings):
    self.settings = settings
    super().__init__(arguments)

  def inspect(self, text, key, value=None, value_style=None):
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
      self.retain(text, value, value_style)


def skip_context_options(arguments):
  return _SkipContextOptions(arguments)


def _apply_output_options(arguments):
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
  arguments = tuple(_ApplyOutputOptions(arguments, settings))

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


def make_output_plan(arguments, result_action=None):
  arguments = tuple(arguments)
  if result_action is None or not result_action.machine_output:
    return _apply_output_options(arguments)

  keep_paths = False
  if result_action.index is not None and not result_action.json:
    requested_output_plan, _ = _apply_output_options(arguments)
    keep_paths = requested_output_plan.mode is GrepOutputMode.PATHS
  return _apply_output_options(_SkipIncompatibleOutputOptions(arguments, keep_paths))


class _LeadingGitOptions(ArgumentIterator):
  non_option_arguments = frozenset(("+debug", "+print", "+t", "+to", "(", ")"))

  def __init__(self, arguments, parser):
    self.parser = parser
    self.leading = True
    self.patterns = []
    self.has_pattern_source = False
    self.all_patterns_known = True
    super().__init__(arguments)

  def inspect(self, text, key, value=None, value_style=None):
    if not self.leading:
      return
    if not text.startswith("-") and text not in self.non_option_arguments:
      self.leading = False
      return
    if value_style == VS.SPACED and value is None:
      self.parser.error(f"{text} requires an argument")

    if key in ("-e", _MASKED_PATTERN_OPTION):
      self.patterns.append(value)
      self.has_pattern_source = True
    elif key in ("-f", "--file"):
      self.has_pattern_source = True
      self.all_patterns_known = False
    if key == _MASKED_PATTERN_OPTION:
      text = "-e"
    self.retain(text, value, value_style)


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


def _pop_trailing_result_idx(arguments):
  if arguments and (match := re.fullmatch(r"/(\d+)", arguments[-1])):
    return arguments[:-1], int(match.group(1))
  return arguments, None


class _MaskPatternOptions(ArgumentIterator):
  def inspect(self, text, key, value=None, value_style=None):
    if key in ("-e", "--pattern"):
      self.retain(_MASKED_PATTERN_OPTION, value, VS.SPACED)
    else:
      self.retain(text, value, value_style)


def parse_search_arguments(parser, arguments=None, *, pattern_metavar):
  arguments = sys.argv[1:] if arguments is None else arguments
  arguments = list(_MaskPatternOptions(arguments))
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

  pattern_value = options.pattern
  if has_pattern_source:
    command_patterns = ()
    pattern_texts = grep_patterns
    result_idx = None
    scope_arguments = operands
  elif pattern_value is not None:
    command_patterns = (pattern_value,)
    pattern_texts = command_patterns
    result_idx = None
    scope_arguments = operands
  else:
    if not operands:
      parser.error(f"{pattern_metavar} is required")
    pattern_value, result_idx = split_result_idx(operands[0])
    command_patterns = (pattern_value,)
    pattern_texts = command_patterns
    scope_arguments = operands[1:]

  if any(argument.startswith("-") for argument in scope_arguments):
    parser.error(f"git grep options must precede {pattern_metavar}")
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
    parser.error(f"--open requires --show or {pattern_metavar}/N")
  if pattern_value == "":
    parser.error(f"{pattern_metavar.lower()} cannot be empty")
  if scope_arguments is not None and options.root is not None:
    parser.error("--root cannot be combined with revisions or paths")
  if scope_arguments is not None and options.ext is not None:
    parser.error("--ext cannot be combined with revisions or paths")

  options.pattern = pattern_value
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
