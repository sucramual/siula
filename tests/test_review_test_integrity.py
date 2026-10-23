import pytest

from siula.review.test_integrity import MARKER, is_test_file


@pytest.mark.parametrize(
    "text",
    [
        pytest.param('@pytest.mark.skip(reason="needs a sandbox")', id="pytest-skip-mark"),
        pytest.param("@pytest.mark.skipif(sys.platform == 'win32')", id="pytest-skipif-mark"),
        pytest.param("@pytest.mark.xfail", id="pytest-xfail-mark"),
        pytest.param('pytest.xfail("later")', id="pytest-xfail-call"),
        pytest.param('pytest.skip("no docker")', id="pytest-skip-call"),
        pytest.param('np = pytest.importorskip("numpy")', id="pytest-importorskip"),
        pytest.param('@unittest.skip("later")', id="unittest-skip"),
        pytest.param("@unittest.expectedFailure", id="unittest-expected-failure"),
        pytest.param('self.skipTest("later")', id="unittest-skiptest-call"),
        pytest.param('raise unittest.SkipTest("later")', id="unittest-raise-skiptest"),
        pytest.param('it.only("adds", () => {', id="jest-it-only"),
        pytest.param('describe.skip("app", () => {});', id="jest-describe-skip"),
        pytest.param('test.todo("subtracts");', id="jest-test-todo"),
        pytest.param('xit("adds", () => {});', id="jasmine-xit"),
        pytest.param('fdescribe("app", () => {});', id="jasmine-fdescribe"),
        pytest.param('fit("adds", () => {});', id="jasmine-fit"),
        pytest.param('\tt.Skip("later")', id="go-t-skip"),
        pytest.param('\tb.Skipf("no %s", name)', id="go-b-skipf"),
        pytest.param("\tt.SkipNow()", id="go-skipnow"),
        pytest.param("#[ignore]", id="rust-ignore"),
        pytest.param('#[ignore = "slow"]', id="rust-ignore-reason"),
        pytest.param("  @Disabled", id="java-disabled"),
        pytest.param("  @Ignore", id="java-ignore"),
    ],
)
def test_marker_matches_skip_focus_and_expected_failure(text: str):
    assert MARKER.search(text) is not None


@pytest.mark.parametrize(
    "text",
    [
        pytest.param('return rows.only("id")', id="queryset-only"),
        pytest.param("model.fit([1])", id="model-fit"),
        pytest.param("total = profit(margin)", id="word-ending-in-fit"),
        pytest.param("this.it.only(1)", id="member-named-it"),
        pytest.param('describe("only the id", () => {});', id="only-in-a-name"),
        pytest.param("assert result.skipped == 0", id="skipped-count"),
        pytest.param("def test_skip_reason_is_kept():", id="test-name-with-skip"),
    ],
)
def test_marker_does_not_match_ordinary_code(text: str):
    assert MARKER.search(text) is None


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_x.py",
        "test_x.py",
        "src/app/x_test.py",
        "pkg/x_test.go",
        "web/a.test.ts",
        "web/a.spec.tsx",
        "src/test/java/AppTest.java",
        "app/src/main/AppTests.java",
        "app/src/AppTest.kt",
        "web/__tests__/a.js",
        "spec/models/user.rb",
        "e2e/login.ts",
        "tests/fixtures/data.json",
    ],
)
def test_is_test_file_recognizes_test_paths(path: str):
    assert is_test_file(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "src/x.py",
        "notes/x.txt",
        "src/testing/x.py",
        "src/latest.py",
        "src/contest_entry.py",
        "docs/tests",
        "README.md",
    ],
)
def test_is_test_file_refuses_other_paths(path: str):
    assert is_test_file(path) is False
