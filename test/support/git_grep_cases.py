from dataclasses import dataclass


@dataclass(frozen=True)
class GitGrepCase:
  name: str
  command: tuple[str, ...]
  mode: str


def _case(name, arguments, *, mode="matches", color="never"):
  command = (
    "git", "grep", f"--color={color}", "-I", "-H", "-n", "--column", "-z",
    *arguments,
  )
  return GitGrepCase(name, command, mode)


def _search(name, pattern="target", *, grep_args=(), paths=("valid_code.py",), patterns=None, word=True, mode="matches", color="never", scope=None):
  arguments = []
  if word:
    arguments.append("-w")
  arguments.extend(grep_args)
  for expression in patterns or (pattern,):
    arguments.extend(("-e", expression))
  arguments.extend(scope if scope is not None else ("--", *paths))
  return _case(name, arguments, mode=mode, color=color)


GIT_GREP_CASES = (
  _search("word_matches"),
  _search("colored_matches", color="always"),
  _search("regex_slash", "route/123", word=False),
  _search("no_matches", "missing_name"),
  _search("invalid_option", grep_args=("--bad-git-option",)),
  _search("import_name", "os"),
  _search("import_statement", "import os"),
  _search("multiline_import", "ImportedMarker"),
  _search("malformed_import", "UncertainMarker", paths=("syntax_errors.py",)),
  _search("after_context_spaced", "os", grep_args=("-A", "2"), mode="context"),
  _search("before_context_attached", grep_args=("-B3",), mode="context"),
  _search("numeric_context", grep_args=("-2",), mode="context"),
  _search("centered_context", "os", grep_args=("-C", "2"), mode="context"),
  _search("show_function", grep_args=("--show-function",), mode="context"),
  _search("function_context", grep_args=("-W",), mode="context"),
  _search("abbreviated_before_context", grep_args=("--bef=2",), mode="context"),
  _search("abbreviated_show_function", grep_args=("--show-f",), mode="context"),
  _search("abbreviated_function_context", grep_args=("--function-con",), mode="context"),
  _search("case_insensitive_context_cluster", grep_args=("-in", "-A2"), mode="context"),
  _search("paths", "decimal", grep_args=("-l",), mode="paths"),
  _search("paths_take_precedence", grep_args=("-c", "-l"), mode="paths"),
  _search("files_without_matches", grep_args=("-L",), mode="paths"),
  _search("counts", "decimal", grep_args=("-c",), mode="counts"),
  _search("counts_with_context", grep_args=("-c", "-A", "2"), mode="counts"),
  _search("counts_with_clustered_options", grep_args=("-iA2", "-c"), mode="counts"),
  _search("multiple_patterns", patterns=("AlphaMarker", "BetaMarker")),
  _case(
    "boolean_patterns",
    ("-w", "-e", "AlphaMarker", "--and", "(", "-e", "BetaMarker", ")", "--", "valid_code.py"),
  ),
  _search("perl_case_insensitive", "alphamarker", grep_args=("-iP",)),
  _search("revision_and_path", scope=("HEAD", "valid_code.py")),
  _search("occurrences", grep_args=("-o",)),
  _search("case_insensitive_occurrences", grep_args=("-i", "-o")),
  _case(
    "complex_occurrence_options",
    ("-w", "-i", "-n", "--no-context", "--max-depth", "7", "-e", "-W", "-o", "-e", "target", "--", "valid_code.py"),
  ),
  _case(
    "clustered_pattern_occurrences",
    ("-ie", "needle", "-o", "--", "valid_code.py"),
  ),
  _case(
    "empty_pattern_occurrences",
    ("-e", "", "-o", "--", "valid_code.py"),
  ),
  _search("max_depth_occurrences", grep_args=("--max-d", "4", "-o")),
  _search("threaded_occurrences", grep_args=("--threads=3", "-o")),
  _search("invalid_occurrence_option", grep_args=("--123", "-o")),
  _search("invalid_show_function_assignment", grep_args=("--show-function=value", "-o")),
  _search("quoted_occurrences", "route/123", grep_args=("-o",), word=False),
  _search("repeated_line_occurrences", "Render", grep_args=("-o",), paths=("valid_code.go",)),
  _search("typescript_definitions", "DataStore", grep_args=("-o",), paths=("valid_code.ts",)),
  _search("go_definitions", "Tree", grep_args=("-o",), paths=("valid_code.go",)),
  _search("empty_pattern", "", word=False),
  _search("max_depth_and_threads", grep_args=("--max-d", "4", "--threads=2")),
  _case(
    "option_like_operands",
    ("-w", "-e", "target", "--", "-A", "2", ""),
  ),
  _case(
    "leading_pattern_sources",
    ("-iA", "2", "-efirst_expression", "-fpatterns.txt", "fallback_expression", "source"),
  ),
  _case(
    "pattern_value_and_option_like_operands",
    ("-e", "-C", "--", "-A", "3"),
  ),
  _search("path_mode_aliases", grep_args=("-ilL", "--name-only", "--files-without-match"), mode="paths"),
  _search("invalid_column_assignment", grep_args=("--column=value",)),
  _search("invalid_color_assignment", grep_args=("--color=value",)),
  _case(
    "pattern_and_file_values",
    ("-e", "--column", "-f", "-h", "--max-depth", "3", "--no-count", "--", "valid_code.py"),
  ),
)


def validate_cases(cases=GIT_GREP_CASES):
  names = [case.name for case in cases]
  if len(names) != len(set(names)):
    raise ValueError("git grep case names must be unique")
  commands = [case.command for case in cases]
  if len(commands) != len(set(commands)):
    raise ValueError("git grep case commands must be unique")
