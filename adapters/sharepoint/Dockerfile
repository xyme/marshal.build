# marshal SharePoint MCP adapter — build context = adapters/sharepoint/.
# Deployed INDEPENDENTLY of the platform's hardened release path (the
# adapter is infrastructure the operator runs; see README.md).
FROM public.ecr.aws/docker/library/python:3.12-slim

# Lambda Web Adapter: the SAME image runs as a Lambda container (function
# URL) or on any container host — the recommended beta-drill hosting is a
# control-plane Lambda (design §4). Inert outside Lambda.
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.9.1 /lambda-adapter /opt/extensions/lambda-adapter
ENV PORT=8080

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY adapter.py graph.py extract.py ./

EXPOSE 8080
# Container hosts (ECS/App Runner/Lambda web adapter) override as needed.
CMD ["python", "adapter.py"]
