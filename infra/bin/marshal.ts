#!/usr/bin/env node
import * as cdk from "aws-cdk-lib";
import { MarshalAppStack } from "../lib/app-stack";
import { MarshalAuthStack } from "../lib/auth-stack";
import { MarshalBuildStack } from "../lib/build-stack";
import { MarshalDataStack } from "../lib/data-stack";
import { MarshalMarketingStack } from "../lib/marketing-stack";
import { MarshalNetworkStack } from "../lib/network-stack";
import { resolveMarshalTier } from "../lib/tier";

const app = new cdk.App();

// Region is fixed to us-east-1 per product decision (full Claude access).
// Account resolves from credentials at deploy; synth stays account-agnostic for CI.
const env = { region: "us-east-1" };

// Sizing tier (G18): `-c marshalTier=evaluate` or MARSHAL_TIER=evaluate from
// the overlay via scripts/deploy-env.sh; unset = production (today's sizing).
const tier = resolveMarshalTier(app.node.tryGetContext("marshalTier") ?? process.env.MARSHAL_TIER);

const persistentStackProps = { env, terminationProtection: true };

const network = new MarshalNetworkStack(app, "MarshalNetworkStack", persistentStackProps);
const auth = new MarshalAuthStack(app, "MarshalAuthStack", persistentStackProps);
const data = new MarshalDataStack(app, "MarshalDataStack", {
  ...persistentStackProps,
  vpc: network.vpc,
  tier,
});
const build = new MarshalBuildStack(app, "MarshalBuildStack", persistentStackProps);
new MarshalAppStack(app, "MarshalAppStack", {
  ...persistentStackProps,
  tier,
  vpc: network.vpc,
  db: data.db,
  dbSecurityGroup: data.dbSecurityGroup,
  chatTable: data.chatTable,
  runtimeStateTable: data.runtimeStateTable,
  backendRepo: build.backendRepo,
  frontendRepo: build.frontendRepo,
});
// B22 P0: the marketing site is the OWNER'S property, not part of a generic
// installation — the stack registers only when its certificate is supplied.
if (process.env.MARKETING_CERT_ARN) {
  new MarshalMarketingStack(app, "MarshalMarketingStack", { env });
}
void auth;

cdk.Tags.of(app).add("marshal-ai:component", "control-plane");
