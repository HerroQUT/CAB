import aws_cdk as cdk
from pipeline_stack import PipelineStack

app = cdk.App()
sid = app.node.try_get_context("studentId")
PipelineStack(app, f"{sid}-StrobeA2Pipeline",
              env=cdk.Environment(region="ap-southeast-2"))
app.synth()
