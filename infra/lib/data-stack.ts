import * as cdk from "aws-cdk-lib";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as rds from "aws-cdk-lib/aws-rds";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import { Construct } from "constructs";
import type { MarshalTier } from "./tier";

export interface MarshalDataStackProps extends cdk.StackProps {
  vpc: ec2.IVpc;
  /** Sizing tier (G18); default production. Only multiAz moves with it. */
  tier?: MarshalTier;
}

/**
 * Data layer (FSD §4.1.9 / §5.2):
 *  - DynamoDB chat messages table
 *  - RDS Postgres 16 in PRIVATE subnets of the shared VPC — reachable only from
 *    security groups that are explicitly granted (backend service). The former
 *    public-but-IP-locked dev posture is retired with the cloud deployment.
 */
export class MarshalDataStack extends cdk.Stack {
  public readonly chatTable: dynamodb.Table;
  public readonly runtimeStateTable: dynamodb.Table;
  public readonly db: rds.DatabaseInstance;
  public readonly dbSecurityGroup: ec2.SecurityGroup;

  constructor(scope: Construct, id: string, props: MarshalDataStackProps) {
    super(scope, id, props);

    this.chatTable = new dynamodb.Table(this, "ChatMessages", {
      tableName: "marshal-chat-messages",
      partitionKey: { name: "session_id", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "sk", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      deletionProtection: true,
      // Private-beta account/transcript data is durable. Explicit teardown and
      // retention workflows own deletion; a stack delete must not erase it.
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // S12: shared runtime counters (rate windows, spend MTD) — atomic
    // UpdateItem ADD across N backend tasks; rows self-expire via TTL.
    this.runtimeStateTable = new dynamodb.Table(this, "RuntimeState", {
      tableName: "marshal-runtime-state",
      partitionKey: { name: "pk", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: "expires",
      removalPolicy: cdk.RemovalPolicy.DESTROY, // rebuildable cache-tier data
    });

    this.dbSecurityGroup = new ec2.SecurityGroup(this, "DbSecurityGroup", {
      vpc: props.vpc,
      // Keeps the AS-DEPLOYED wording ON PURPOSE (not lowercased with the
      // rebrand): GroupDescription is immutable, so editing it replaces the
      // security group — and this group's id is an export MarshalAppStack
      // imports, which CloudFormation refuses to change while in use (live
      // finding, 3 Aug 2026: the rebrand's first deploy rolled DataStack back
      // on exactly this). Cosmetic text is not worth replacing the database
      // security group. Verified against the deployed template.
      description: "Marshal Postgres - private; ingress granted per consuming service",
      allowAllOutbound: true,
    });

    // Construct id "PostgresPrivate" (not "Postgres"): forces REPLACEMENT of the
    // instance + subnet group — RDS cannot move a subnet group across VPCs in place.
    // Secret name is auto-generated: the old fixed-name secret enters its deletion
    // recovery window on cleanup and would collide with a reused name.
    this.db = new rds.DatabaseInstance(this, "PostgresPrivate", {
      engine: rds.DatabaseInstanceEngine.postgres({
        version: rds.PostgresEngineVersion.VER_16,
      }),
      instanceType: ec2.InstanceType.of(ec2.InstanceClass.T4G, ec2.InstanceSize.MICRO),
      vpc: props.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      publiclyAccessible: false,
      securityGroups: [this.dbSecurityGroup],
      credentials: rds.Credentials.fromGeneratedSecret("marshal"),
      databaseName: "marshal",
      allocatedStorage: 20,
      storageType: rds.StorageType.GP3,
      // S13-03 (product decision D10): multi-AZ removes the single-AZ database as
      // an event-day failure mode (~+$15/mo). Failover is automatic; the app
      // reconnects through the same endpoint DNS. The evaluate tier (G18)
      // runs single-AZ: an evaluation does not need the standby.
      multiAz: (props.tier ?? "production") !== "evaluate",
      deletionProtection: true,
      backupRetention: cdk.Duration.days(7), // S13-03: 1d → 7d for PITR headroom
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      cloudwatchLogsExports: ["postgresql"],
    });

    this.db.secret?.applyRemovalPolicy(cdk.RemovalPolicy.RETAIN);
    // DatabaseInstance.secret resolves through the target attachment. Retain
    // the generated AWS::SecretsManager::Secret itself as well, otherwise a
    // deliberate stack delete could retain RDS while deleting its credential.
    for (const construct of this.node.findAll()) {
      if (construct instanceof secretsmanager.CfnSecret) {
        construct.applyRemovalPolicy(cdk.RemovalPolicy.RETAIN);
      }
    }

    new cdk.CfnOutput(this, "ChatTableName", { value: this.chatTable.tableName });
    new cdk.CfnOutput(this, "DbEndpoint", { value: this.db.dbInstanceEndpointAddress });
    new cdk.CfnOutput(this, "DbSecretArn", { value: this.db.secret?.secretArn ?? "" });
  }
}
