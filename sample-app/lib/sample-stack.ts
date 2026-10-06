import * as cdk from "aws-cdk-lib";
import * as apigwv2 from "aws-cdk-lib/aws-apigatewayv2";
import { HttpLambdaIntegration } from "aws-cdk-lib/aws-apigatewayv2-integrations";
import * as lambda from "aws-cdk-lib/aws-lambda";
import { Construct } from "constructs";

/**
 * FSD §4.5.5K Sprint 1 sample app: hardcoded Hello World Lambda + API Gateway.
 * Lambda code is INLINE so the template is asset-free (deployable via CreateStack
 * with no S3 staging — works in unbootstrapped sandbox accounts).
 */
export class MarshalSampleAppStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    const handler = new lambda.Function(this, "HelloHandler", {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "index.handler",
      timeout: cdk.Duration.seconds(10),
      code: lambda.Code.fromInline(
        [
          "import json",
          "import os",
          "",
          "def handler(event, context):",
          "    return {",
          "        'statusCode': 200,",
          "        'headers': {'Content-Type': 'application/json'},",
          "        'body': json.dumps({",
          "            'message': 'Hello from marshal',",
          "            'service': 'sample-app',",
          "            'region': os.environ.get('AWS_REGION', 'unknown'),",
          "        }),",
          "    }",
        ].join("\n")
      ),
    });

    const api = new apigwv2.HttpApi(this, "HelloApi", {
      apiName: "marshal-sample-hello",
      defaultIntegration: new HttpLambdaIntegration("HelloIntegration", handler),
    });

    new cdk.CfnOutput(this, "ApiUrl", { value: api.apiEndpoint });
  }
}
