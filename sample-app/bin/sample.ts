#!/usr/bin/env node
import * as cdk from "aws-cdk-lib";
import { MarshalSampleAppStack } from "../lib/sample-stack";

const app = new cdk.App();

new MarshalSampleAppStack(app, "MarshalSampleApp", {
  // BootstraplessSynthesizer: template has no bootstrap parameters/rules, so the
  // backend can deploy it via plain CloudFormation CreateStack into ANY account
  // (including freshly-leased sandbox accounts with no CDK bootstrap).
  synthesizer: new cdk.BootstraplessSynthesizer(),
});
