# Test-integrity checklist

Apply this list to every test that the diff adds or changes. Report each failed item as a `high` finding, or as `blocker` when the test hides a failure that would otherwise stop the merge. The repository's own guidance can narrow an item.

A test fails review when it:

- compares a value with itself, or with a copy of itself;
- reads source text or copies a list from the code, where it should run the behaviour;
- checks how the code works, such as call order, private names, or exact command strings, where it should check what the code produces;
- repeats a contract that another test already pins;
- needs an export, flag, or hook that exists only for tests;
- computes its expected value with the code under test;
- uses a mock that supplies the same behaviour that the test claims to check;
- uses test data with receipts, orderings, or storage paths that the real code never writes.

Also check that:

- a test that expects a refusal reaches the intended guard, asserts the specific error, and shows that nothing after the guard ran;
- a test that asserts a value is absent from a record also asserts that the record exists;
- each test name describes the behaviour that the test runs;
- each new export, flag, or hook has a production caller.

A new `skip`, `only`, `xfail`, or similar marker is a `high` finding. So is a removed or renamed test file, unless the intent says why the test goes away.

This list adapts ideas from OpenClaw's [test-audit skill](https://github.com/openclaw/openclaw/blob/main/.agents/skills/test-audit/SKILL.md).
