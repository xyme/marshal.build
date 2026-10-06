import * as path from "node:path";
import * as cdk from "aws-cdk-lib";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as cloudfront from "aws-cdk-lib/aws-cloudfront";
import * as origins from "aws-cdk-lib/aws-cloudfront-origins";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as s3deploy from "aws-cdk-lib/aws-s3-deployment";
import { Construct } from "constructs";

/**
 * Product marketing site at https://marshal.build (apex):
 * private S3 bucket + OAC + CloudFront with the pre-issued apex certificate.
 * Content lives in marketing/site/ and is synced on every deploy
 * (BucketDeployment prunes stale files and invalidates the distribution).
 *
 * The app keeps its own distribution (MarshalAppStack).
 */
// B22 P0: no owner defaults — bin/marshal.ts only registers this stack when
// MARKETING_CERT_ARN is supplied (the overlay pins both for this install).
const APEX_DOMAIN = process.env.MARKETING_APEX_DOMAIN ?? "marshal.build";
const APEX_CERT_ARN = process.env.MARKETING_CERT_ARN ?? "";

export class MarshalMarketingStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    const bucket = new s3.Bucket(this, "SiteBucket", {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    const distribution = new cloudfront.Distribution(this, "Distribution", {
      comment: "marshal marketing site",
      defaultRootObject: "index.html",
      domainNames: [APEX_DOMAIN],
      certificate: acm.Certificate.fromCertificateArn(this, "ApexCert", APEX_CERT_ARN),
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(bucket),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
      },
      errorResponses: [
        // OAC'd S3 returns 403 for missing keys; both map to the themed 404 page
        { httpStatus: 403, responseHttpStatus: 404, responsePagePath: "/404.html" },
        { httpStatus: 404, responseHttpStatus: 404, responsePagePath: "/404.html" },
      ],
    });

    new s3deploy.BucketDeployment(this, "SiteContent", {
      sources: [s3deploy.Source.asset(path.join(__dirname, "..", "..", "marketing", "site"))],
      destinationBucket: bucket,
      distribution,
      distributionPaths: ["/*"],
      prune: true,
    });

    new cdk.CfnOutput(this, "MarketingUrl", { value: `https://${APEX_DOMAIN}` });
    new cdk.CfnOutput(this, "DistributionDomain", {
      value: distribution.distributionDomainName,
      description: "Point marshal.build (ALIAS/ANAME or CNAME-flattened A) at this",
    });
  }
}
