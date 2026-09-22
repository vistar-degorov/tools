"""Deliberately invalid input for manually testing badimport.py."""

from __future__ import annotations

import collections.abc
import json as json_module
import math as duplicate_math
import math as duplicate_math
import os
import sys
from collections import (
  Counter as RenamedCounter,
  deque,
)
from pathlib import Path

# Neither of these should be interpreted as an import.
# import ignored_comment
IMPORT_EXAMPLE = "import ignored_string"

# These imports are used.
print(collections.abc)
print(duplicate_math.pi)
print(json_module.dumps({"cwd": os.getcwd()}))
print(Path("."))
print(deque())

# Syntax errors do not normally stop the tokenizer.
if :
  import unused_inside_broken_if

broken_assignment =
import unused_after_broken_assignment

# An inconsistent dedent makes tokenize raise IndentationError. badimport
# should skip the offending line and resume with the following import.
if True:
  correctly_indented = True
 incorrectly_indented = False
import unused_after_bad_indent

# A second inconsistent dedent forces another tokenizer restart, producing
# a third token segment for badimport to analyze.
if True:
  also_correctly_indented = True
 incorrectly_indented_again = False
import unused_after_second_bad_indent

# The unmatched opening bracket makes tokenize raise TokenError at EOF.
# Imports on later physical lines should still be recovered.
broken_collection = (
import unused_after_unclosed_bracket
