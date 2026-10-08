/**
 * The release synthesis gate's comparison: a value that changes on every synthesis is only excused
 * once a source run shows it changing, and a real difference is still reported after confirmation.
 * Source runs here come from fixed sequences, so the outcomes are deterministic.
 */

import assert from 'node:assert/strict';
import test from 'node:test';

import { CONFIRMING_SOURCE_RUNS, confirmedProblems, unexpectedDifferences } from '../support/release-synth-gate.ts';

/** A template whose name ends in two random hex digits, like the analytics target group name. */
const template = (suffix: string, stable = 'same') => ({ Resources: { group: { Name: `idea-test1-dashboard-d60b7716-${suffix}` }, other: stable } });

/** Source runs that yield the given suffixes in order. */
function sourceSequence(suffixes: string[], stable = 'same'): { next: () => Promise<ReturnType<typeof template>>; calls: () => number } {
  let index = 0;
  return {
    next: async () => template(suffixes[index++] ?? suffixes[suffixes.length - 1], stable),
    calls: () => index,
  };
}

const problemsOf = (sources: unknown[], executable: unknown) => unexpectedDifferences(sources, executable);

test('two source runs that agree by chance do not excuse the value on their own', () => {
  assert.equal(unexpectedDifferences([template('b9'), template('b9')], template('49')).length, 1);
  assert.deepEqual(unexpectedDifferences([template('b9'), template('5f')], template('49')), []);
});

test('a low-entropy value is confirmed as changing by one more source run', async () => {
  const more = sourceSequence(['b9', '10']);
  const result = await confirmedProblems([template('b9'), template('b9')], template('49'), problemsOf, more.next);
  assert.deepEqual(result.problems, []);
  assert.equal(result.sourceRuns, 4);
  assert.equal(more.calls(), 2);
});

test('a value every source run agrees on is reported after the bounded confirmation', async () => {
  const more = sourceSequence(['b9']);
  const result = await confirmedProblems([template('b9'), template('b9')], template('49'), problemsOf, more.next);
  assert.equal(result.problems.length, 1);
  assert.match(result.problems[0], /\/Resources\/group\/Name/);
  assert.equal(result.sourceRuns, 2 + CONFIRMING_SOURCE_RUNS);
});

test('a real difference elsewhere is still reported once the changing value is confirmed', async () => {
  const more = sourceSequence(['10', '11']);
  const result = await confirmedProblems([template('b9'), template('b9')], template('49', 'drifted'), problemsOf, more.next);
  assert.deepEqual(result.problems.map((problem) => problem.split(':')[0]), ['/Resources/other']);
});

test('a changing value with a different shape is reported without more source runs helping', async () => {
  const more = sourceSequence(['10']);
  const result = await confirmedProblems([template('b9'), template('5f')], template('4'), problemsOf, more.next);
  assert.equal(result.problems.length, 1);
  assert.match(result.problems[0], /different shape/);
});

test('matching output needs no extra source runs', async () => {
  const more = sourceSequence(['10']);
  const result = await confirmedProblems([template('b9'), template('b9')], template('b9'), problemsOf, more.next);
  assert.deepEqual(result, { problems: [], sourceRuns: 2 });
  assert.equal(more.calls(), 0);
});
