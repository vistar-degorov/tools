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
"import ignored_string"
"from ignore.me import ignore_me"

# weird syntax
from weird.syntax import "ignore_me", ignore_me_2
from weird.syntax import unused_before_string, "ignore_me"
from weird.syntax import unused_comma , , , ,


# These imports are used.
print(collections.abc)
print(duplicate_math.pi)
print(json_module.dumps({"cwd": os.getcwd()}))
print(Path("."))
print(deque())

# Syntax errors do not normally stop the tokenizer.
if :
  import unused_inside_broken_if

# An incomplete from-import should be ignored.
from operator
from operator import

broken_assignment =
import unused_after_broken_assignment

# Leading indentation is removed before tokenization, so inconsistent dedents
# should not prevent the following imports from being analyzed.
if True:
  correctly_indented = True
 incorrectly_indented = False
import unused_after_bad_indent

# Include another inconsistent dedent to exercise the same normalization.
if True:
  also_correctly_indented = True
 incorrectly_indented_again = False
import unused_after_second_bad_indent

# The unmatched opening bracket makes tokenize raise TokenError at EOF.
# Imports on later physical lines should still be recovered.
broken_collection = (
import unused_after_unclosed_bracket
