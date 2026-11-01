from dataclasses import dataclass


@dataclass(frozen=True)
class GitGrepCase:
  name: str
  command: tuple[str, ...]
  mode: str
  covers: tuple[str, ...] = ()


def _case(name, arguments, *, mode="matches", covers=(), color="never"):
  command = (
    "git", "grep", f"--color={color}", "-I", "-H", "-n", "--column", "-z",
    *arguments,
  )
  return GitGrepCase(name, command, mode, tuple(covers))


def _search(name, pattern="target", *, grep_args=(), paths=("valid_code.py",), patterns=None, word=True, mode="matches", covers=(), color="never", scope=None):
  arguments = []
  if word:
    arguments.append("-w")
  arguments.extend(grep_args)
  for expression in patterns or (pattern,):
    arguments.extend(("-e", expression))
  arguments.extend(scope if scope is not None else ("--", *paths))
  return _case(name, arguments, mode=mode, covers=covers, color=color)


GIT_GREP_CASES = (
  _search(
    "word_matches",
    covers=(
      "test_default_word_search",
      "test_json_output",
      "test_human_and_strict_selection",
      "test_output_plan_tracks_visible_fields",
      "test_output_plan_honors_negated_options",
    ),
  ),
  _search("colored_matches", color="always", covers=("test_terminal_output_is_colored", "test_explicit_color_and_quiet_output")),
  _search("regex_slash", "route/123", word=False, covers=("test_explicit_pattern_disables_slash_selection",)),
  _search("no_matches", "missing_name", covers=("test_exit_codes_distinguish_matches_and_errors",)),
  _search("invalid_option", grep_args=("--bad-git-option",), covers=("test_exit_codes_distinguish_matches_and_errors",)),
  _search("import_name", "os", covers=("test_import_and_test_filters",)),
  _search("import_statement", "import os", covers=("test_import_and_test_filters",)),
  _search("multiline_import", "ImportedMarker", covers=("test_completed_multiline_python_import_is_filtered",)),
  _search("malformed_import", "UncertainMarker", paths=("syntax_errors.py",), covers=("test_incomplete_multiline_python_import_is_kept",)),
  _search("after_context_spaced", "os", grep_args=("-A", "2"), mode="context", covers=("test_output_plan_classifies_git_output_modes", "test_context_filter_removes_only_import_only_blocks")),
  _search("before_context_attached", grep_args=("-B3",), mode="context", covers=("test_output_plan_classifies_git_output_modes",)),
  _search("numeric_context", grep_args=("-2",), mode="context", covers=("test_output_plan_classifies_git_output_modes",)),
  _search("centered_context", "os", grep_args=("-C", "2"), mode="context", covers=("test_context_filter_removes_only_import_only_blocks",)),
  _search("show_function", grep_args=("--show-function",), mode="context", covers=("test_output_plan_classifies_git_output_modes", "test_context_flags_do_not_hide_following_presentation_flags")),
  _search("function_context", grep_args=("-W",), mode="context", covers=("test_output_plan_classifies_git_output_modes", "test_context_flags_do_not_hide_following_presentation_flags")),
  _search("abbreviated_before_context", grep_args=("--bef=2",), mode="context", covers=("test_output_plan_recognizes_abbreviated_context_options",)),
  _search("abbreviated_show_function", grep_args=("--show-f",), mode="context", covers=("test_output_plan_recognizes_abbreviated_context_options",)),
  _search("abbreviated_function_context", grep_args=("--function-con",), mode="context", covers=("test_output_plan_recognizes_abbreviated_context_options",)),
  _search("case_insensitive_context_cluster", grep_args=("-in", "-A2"), mode="context", covers=("test_argument_iterator_is_a_pass_through_iterator",)),
  _search("paths", "decimal", grep_args=("-l",), mode="paths", covers=("test_aggregate_modes_are_not_import_filtered", "test_output_plan_tracks_visible_fields")),
  _search("paths_take_precedence", grep_args=("-c", "-l"), mode="paths", covers=("test_output_plan_classifies_git_output_modes",)),
  _search("files_without_matches", grep_args=("-L",), mode="paths", covers=("test_incompatible_output_filter_can_preserve_path_modes",)),
  _search("counts", "decimal", grep_args=("-c",), mode="counts", covers=("test_aggregate_modes_are_not_import_filtered", "test_output_plan_tracks_visible_fields")),
  _search("counts_with_context", grep_args=("-c", "-A", "2"), mode="counts", covers=("test_output_plan_classifies_git_output_modes",)),
  _search("counts_with_clustered_options", grep_args=("-iA2", "-c"), mode="counts", covers=("test_output_preparation_removes_presentation_options",)),
  _search("multiple_patterns", patterns=("AlphaMarker", "BetaMarker"), covers=("test_pattern_options_can_be_repeated_and_mixed", "test_repeated_patterns_support_trailing_result_selection")),
  _case(
    "boolean_patterns",
    ("-w", "-e", "AlphaMarker", "--and", "(", "-e", "BetaMarker", ")", "--", "valid_code.py"),
    covers=("test_pattern_aliases_preserve_expression_order",),
  ),
  _search("perl_case_insensitive", "alphamarker", grep_args=("-iP",), covers=("test_pattern_option_at_end_of_short_option_cluster",)),
  _search("revision_and_path", scope=("HEAD", "valid_code.py"), covers=("test_revision_and_path_arguments_are_left_for_git",)),
  _search("occurrences", grep_args=("-o",), covers=("test_occurrence_options_append_crop_matches_option", "test_output_plan_tracks_crop_matches")),
  _search("case_insensitive_occurrences", grep_args=("-i", "-o"), covers=("test_occurrence_options_append_crop_matches_option", "test_occurrence_options_recognize_abbreviated_long_options")),
  _case(
    "complex_occurrence_options",
    ("-w", "-i", "-n", "--no-context", "--max-depth", "7", "-e", "-W", "-o", "-e", "target", "--", "valid_code.py"),
    covers=("test_occurrence_options_remove_context_options",),
  ),
  _case(
    "clustered_pattern_occurrences",
    ("-ie", "needle", "-o", "--", "valid_code.py"),
    covers=("test_occurrence_options_preserve_spaced_value_for_clustered_option",),
  ),
  _case(
    "empty_pattern_occurrences",
    ("-e", "", "-o", "--", "valid_code.py"),
    covers=("test_occurrence_options_preserve_empty_spaced_value",),
  ),
  _search("max_depth_occurrences", grep_args=("--max-d", "4", "-o"), covers=("test_occurrence_options_preserve_spaced_value_for_abbreviated_option",)),
  _search("threaded_occurrences", grep_args=("--threads=3", "-o"), covers=("test_occurrence_options_do_not_repeat_equals_value",)),
  _search("invalid_occurrence_option", grep_args=("--123", "-o"), covers=("test_occurrence_options_preserve_incomplete_and_invalid_options",)),
  _search("invalid_show_function_assignment", grep_args=("--show-function=value", "-o"), covers=("test_occurrence_options_preserve_invalid_assignments",)),
  _search("quoted_occurrences", "route/123", grep_args=("-o",), word=False, covers=("test_quoted_occurrences_use_byte_columns_and_include_quotes",)),
  _search("repeated_line_occurrences", "Render", grep_args=("-o",), paths=("valid_code.go",), covers=("test_go_definition_occurrences_include_definition_syntax", "test_definition_filter_rejects_go_usages")),
  _search("typescript_definitions", "DataStore", grep_args=("-o",), paths=("valid_code.ts",), covers=("test_definition_filter_handles_typescript_declarations",)),
  _search("go_definitions", "Tree", grep_args=("-o",), paths=("valid_code.go",), covers=("test_definition_filter_handles_simple_go_declarations",)),
  _search("empty_pattern", "", word=False, covers=("test_argument_iterator_is_a_pass_through_iterator",)),
  _search("max_depth_and_threads", grep_args=("--max-d", "4", "--threads=2"), covers=("test_argument_iterator_is_a_pass_through_iterator",)),
  _case(
    "option_like_operands",
    ("-w", "-e", "target", "--", "-A", "2", ""),
    covers=("test_argument_iterator_is_a_pass_through_iterator",),
  ),
  _case(
    "leading_pattern_sources",
    ("-iA", "2", "-efirst_expression", "-fpatterns.txt", "fallback_expression", "source"),
    covers=("test_leading_git_options_use_shared_option_parsing",),
  ),
  _case(
    "pattern_value_and_option_like_operands",
    ("-e", "-C", "--", "-A", "3"),
    covers=("test_incompatible_output_filter_preserves_option_values_and_operands",),
  ),
  _search("path_mode_aliases", grep_args=("-ilL", "--name-only", "--files-without-match"), mode="paths", covers=("test_incompatible_output_filter_can_preserve_path_modes",)),
  _search("invalid_column_assignment", grep_args=("--column=value",), covers=("test_output_preparation_preserves_invalid_assignments",)),
  _search("invalid_color_assignment", grep_args=("--color=value",), covers=("test_output_preparation_preserves_invalid_assignments",)),
  _case(
    "pattern_and_file_values",
    ("-e", "--column", "-f", "-h", "--max-depth", "3", "--no-count", "--", "valid_code.py"),
    covers=("test_output_preparation_preserves_git_options_and_values", "test_output_plan_skips_pattern_option_values"),
  ),
)


def validate_cases(cases=GIT_GREP_CASES):
  names = [case.name for case in cases]
  if len(names) != len(set(names)):
    raise ValueError("git grep case names must be unique")
  commands = [case.command for case in cases]
  if len(commands) != len(set(commands)):
    raise ValueError("git grep case commands must be unique")
