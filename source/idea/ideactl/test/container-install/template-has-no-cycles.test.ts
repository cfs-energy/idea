/**
 * A synthesized stack must have no reference cycle between its resources.
 *
 * Synthesis does not look for one. CloudFormation does, and refuses the whole template at
 * change-set creation with a list of logical ids and nothing else, so a cycle costs a deploy that
 * has already created every stack before it. The container stack shipped with one: the scheduler
 * state file system named its own access point in its file system policy, while the access point
 * named the file system, and no offline check could see it because the template synthesizes
 * perfectly well.
 *
 * The detector walks Ref, Fn::GetAtt and DependsOn, which is the same edge set CloudFormation
 * orders resources by.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { synthEcs } from "../o-constraints/harness.ts";

type Json = Record<string, unknown>;

function isRecord(value: unknown): value is Json {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Every logical id the given value refers to, by Ref or Fn::GetAtt. */
function referencedIds(value: unknown, known: ReadonlySet<string>, found: Set<string>): void {
  if (Array.isArray(value)) {
    for (const item of value) referencedIds(item, known, found);
    return;
  }
  if (!isRecord(value)) return;
  for (const [key, item] of Object.entries(value)) {
    if (key === "Ref" && typeof item === "string") {
      if (known.has(item)) found.add(item);
      continue;
    }
    if (key === "Fn::GetAtt") {
      const target = Array.isArray(item) ? item[0] : String(item).split(".")[0];
      if (typeof target === "string" && known.has(target)) found.add(target);
      continue;
    }
    referencedIds(item, known, found);
  }
}

/** The resource dependency graph CloudFormation orders creation by. */
function dependencyGraph(template: Json): Map<string, Set<string>> {
  const resources = isRecord(template.Resources) ? template.Resources : {};
  const known = new Set(Object.keys(resources));
  const graph = new Map<string, Set<string>>();
  for (const [id, resource] of Object.entries(resources)) {
    const edges = new Set<string>();
    if (isRecord(resource)) {
      referencedIds(resource.Properties, known, edges);
      const dependsOn = resource.DependsOn;
      for (const name of Array.isArray(dependsOn) ? dependsOn : dependsOn === undefined ? [] : [dependsOn]) {
        if (typeof name === "string" && known.has(name)) edges.add(name);
      }
    }
    // A self reference is kept. CloudFormation reports one as a circular dependency, and an
    // inline resource policy naming its own resource is how it happens: the file system policy is
    // a property of the file system, so naming the file system in it resolves an attribute of the
    // resource being defined, so a self edge is a real cycle and must not be discarded.
    graph.set(id, edges);
  }
  return graph;
}

/** The first cycle reachable in the graph, as the path that closes it. */
function findCycle(graph: ReadonlyMap<string, Set<string>>): string[] | undefined {
  const done = new Set<string>();
  const stack: string[] = [];
  const onStack = new Set<string>();

  const walk = (node: string): string[] | undefined => {
    if (onStack.has(node)) return [...stack.slice(stack.indexOf(node)), node];
    if (done.has(node)) return undefined;
    onStack.add(node);
    stack.push(node);
    for (const next of graph.get(node) ?? []) {
      const cycle = walk(next);
      if (cycle !== undefined) return cycle;
    }
    stack.pop();
    onStack.delete(node);
    done.add(node);
    return undefined;
  };

  for (const node of graph.keys()) {
    const cycle = walk(node);
    if (cycle !== undefined) return cycle;
  }
  return undefined;
}

describe("the container stack template", () => {
  it("has no reference cycle between its resources", () => {
    const cycle = findCycle(dependencyGraph(synthEcs()));
    assert.equal(cycle, undefined, `resource cycle: ${cycle?.join(" -> ") ?? ""}`);
  });

  it("has no reference cycle with the observability daemon added", () => {
    const cycle = findCycle(dependencyGraph(synthEcs(true)));
    assert.equal(cycle, undefined, `resource cycle: ${cycle?.join(" -> ") ?? ""}`);
  });

  it("finds a cycle that is there", () => {
    // Without this the check passes whether or not the detector works.
    const planted = dependencyGraph({
      Resources: {
        alpha: { Type: "Test::One", Properties: { other: { Ref: "beta" } } },
        beta: { Type: "Test::Two", Properties: { other: { "Fn::GetAtt": ["alpha", "Arn"] } } },
        gamma: { Type: "Test::Three", Properties: { other: { Ref: "alpha" } } },
      },
    });
    const cycle = findCycle(planted);
    assert.notEqual(cycle, undefined);
    assert.deepEqual(new Set(cycle), new Set(["alpha", "beta"]));
  });

  it("finds a resource that refers to itself", () => {
    // A self-edge-discarding detector reports this shape as clean; CloudFormation refuses it.
    const planted = dependencyGraph({
      Resources: {
        alpha: { Type: "Test::One", Properties: { policy: { "Fn::GetAtt": ["alpha", "Arn"] } } },
      },
    });
    assert.deepEqual(findCycle(planted), ["alpha", "alpha"]);
  });

  it("finds a cycle declared only by DependsOn", () => {
    const planted = dependencyGraph({
      Resources: {
        alpha: { Type: "Test::One", DependsOn: ["beta"] },
        beta: { Type: "Test::Two", DependsOn: "alpha" },
      },
    });
    assert.notEqual(findCycle(planted), undefined);
  });
});
