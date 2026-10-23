"""Recognize test files, and the markers that skip, focus, or expect failure in tests.

A review gate uses is_test_file() to decide which changed paths are tests, and MARKER to count
skip, only, xfail, and similar markers in a file at two revisions. More markers at the head than
at the base means the change hides a test.
"""

import fnmatch
import re
from pathlib import PurePosixPath

TEST_DIRS = {"test", "tests", "__tests__", "spec", "specs", "e2e"}
TEST_NAMES = (
    "test_*.py",
    "*_test.py",
    "*_test.go",
    "*.test.*",
    "*.spec.*",
    "*Test.java",
    "*Tests.java",
    "*Test.kt",
)

# Markers that skip, focus, or expect failure. The lookbehind keeps calls such as model.fit( out.
# It also matches pytest marks without "@" and chains such as test.describe.parallel.only(.
MARKER = re.compile(
    r"@?\bpytest\.mark\.(?:skip|skipif|xfail)\b"
    r"|\bpytest\.(?:skip|xfail|importorskip)\("
    r"|@unittest\.(?:skip|skipIf|skipUnless|expectedFailure)\b"
    r"|\.skipTest\("
    r"|\braise\s+(?:unittest\.)?SkipTest\b"
    r"|(?<![\w.$])(?:it|test|describe|context|suite)"
    r"(?:\.(?:concurrent|sequential|describe|parallel|serial)){0,2}"
    r"\.(?:skip|only|todo|fixme|skipIf|runIf)\b"
    r"|(?<![\w.$])(?:xit|xtest|xdescribe|xcontext|fit|fdescribe|fcontext)\s*\("
    r"|\b[tb]\.Skip(?:f|Now)?\("
    r"|#\[ignore\b"
    r"|@(?:Disabled|Ignore)\b"
)


def is_test_file(path: str) -> bool:
    """Return True when a directory in path is a test directory, or the file name is a test's."""
    parts = PurePosixPath(path)
    return any(part in TEST_DIRS for part in parts.parts[:-1]) or any(
        fnmatch.fnmatchcase(parts.name, pattern) for pattern in TEST_NAMES
    )
