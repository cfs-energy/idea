import assert from "node:assert/strict";
import test from "node:test";
import { ClusterConfig, type ModuleInfo } from "../../src/config/cluster-config.ts";
import { configureSso } from "../../src/cli/commands/sso.ts";

function config(entries: Record<string, unknown>, modules: ModuleInfo[] = []): ClusterConfig {
  return new ClusterConfig(Object.entries(entries).map(([key, value]) => ({ key, value })), modules);
}

const modules: ModuleInfo[] = [
  { module_id: "identity", name: "identity-provider", type: "stack", status: "deployed" },
];

function linkingDeps(users: Array<Record<string, unknown>>, failFor: string[] = []) {
  const writes: Array<[string, unknown]> = [];
  const linked: string[] = [];
  const settings = config({
    "global-settings.module_sets.default.identity-provider.module_id": "identity",
    "identity.cognito.user_pool_id": "pool",
    "identity.cognito.domain_url": "https://login.example.invalid",
    "cluster.load_balancers.external_alb.load_balancer_dns_name": "alb.example.invalid",
    "cluster-manager.server.web_resources_context_path": "/portal",
    "cluster.administrator_username": "clusteradmin",
  }, modules);
  const deps = {
    config: settings,
    setConfigEntry: async (key: string, value: unknown) => { writes.push([key, value]); },
    sleep: async () => {},
    out: () => {},
    cognito: {
      getIdentityProviderByIdentifier: async () => { throw Object.assign(new Error("missing"), { name: "ResourceNotFoundException" }); },
      createIdentityProvider: async () => {},
      updateIdentityProvider: async () => {},
      createUserPoolClient: async () => ({ UserPoolClient: { ClientId: "client", ClientSecret: "secret" } }),
      updateUserPoolClient: async () => {},
      listUsers: async () => ({ Users: users }),
      adminLinkProviderForUser: async (input: { DestinationUser?: { ProviderAttributeValue?: string } }) => {
        const name = input.DestinationUser?.ProviderAttributeValue ?? "";
        if (failFor.includes(name)) throw new Error("link refused");
        linked.push(name);
      },
    },
    secrets: {
      describeSecret: async () => { throw Object.assign(new Error("missing"), { name: "ResourceNotFoundException" }); },
      createSecret: async () => ({ ARN: "secret-id" }),
      updateSecret: async () => {},
    },
  };
  const options = {
    clusterName: "sample-cluster", providerName: "provider", providerType: "OIDC", providerEmailAttribute: "mail",
    oidcClientId: "client-input", oidcClientSecret: "secret-input", oidcIssuer: "https://issuer.example.invalid",
  };
  return { deps, options, writes, linked };
}

function poolUser(name: string, identities?: unknown) {
  const attributes = [{ Name: "email", Value: `${name}@example.invalid` }];
  if (identities !== undefined) attributes.push({ Name: "identities", Value: JSON.stringify(identities) });
  return { Username: name, Attributes: attributes };
}

test("sso links every account but the administrator's exact name, and skips linked ones", async () => {
  const { deps, options, writes, linked } = linkingDeps([
    poolUser("clusteradmin"),
    poolUser("clusteradmin2"),
    poolUser("admin"),
    poolUser("cluster"),
    poolUser("already", [{ providerName: "provider", userId: "already@example.invalid" }]),
  ]);
  await configureSso(deps as never, options);
  assert.deepEqual(linked, ["clusteradmin2", "admin", "cluster"]);
  assert.equal(writes.at(-1)?.[0], "identity.cognito.sso_enabled");
});

test("sso is not enabled while any account could not be linked", async () => {
  const { deps, options, writes, linked } = linkingDeps([poolUser("member-a"), poolUser("member-b")], ["member-a"]);
  await assert.rejects(configureSso(deps as never, options), /could not be linked: member-a/);
  assert.deepEqual(linked, ["member-b"]);
  assert.ok(!writes.some(([key]) => key === "identity.cognito.sso_enabled"));
});
