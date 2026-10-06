#!/usr/bin/env node
// Templated by marshal (cdk-artifacts spec R1.2) — models never touch this.
// LegacyStackSynthesizer: assets surface as CFN parameters the platform's
// deployer satisfies from an Enclave-local staging bucket — zero `cdk
// bootstrap` requirement in leased accounts (spec scope stance).
import * as cdk from "aws-cdk-lib";
import { AppStack } from "../lib/app-stack";

const app = new cdk.App({
  defaultStackSynthesizer: new cdk.LegacyStackSynthesizer(),
});
new AppStack(app, "GeneratedApp");
