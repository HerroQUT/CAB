"""One CloudWatch dashboard for the scaling and caching evidence.

    python scripts/make_dashboard.py --student-id n11457660

Widgets: queue depth vs. worker tasks (the scaling story on one graph),
dead-letter queue, worker CPU, and CloudFront requests and bytes. Open it
during the load test and screen-record it for the video.
"""
from __future__ import annotations

import argparse
import json

import boto3
from botocore.exceptions import ClientError

REGION = "ap-southeast-2"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--student-id", required=True)
    args = ap.parse_args()
    sid = args.student_id
    s = boto3.Session(region_name=REGION)
    queue, dlq = f"{sid}-strobe-classify", f"{sid}-strobe-classify-dlq"
    cluster, service = f"{sid}-strobe-a2", f"{sid}-strobe-worker"

    try:
        dist = s.client("ssm").get_parameter(Name=f"/{sid}/strobe-a2/mcp/cdn-distribution-id")["Parameter"]["Value"]
    except ClientError:
        dist = None

    def widget(x, y, w, h, title, metrics, region=REGION, stat="Maximum", right=None):
        props = {"title": title, "region": region, "metrics": metrics, "stat": stat,
                 "period": 60, "view": "timeSeries", "stacked": False}
        if right:
            props["yAxis"] = {"left": {"min": 0}, "right": {"min": 0, "label": right}}
        return {"type": "metric", "x": x, "y": y, "width": w, "height": h, "properties": props}

    widgets = [
        widget(0, 0, 16, 7, "Work queue vs. worker tasks", [
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", queue, {"label": "waiting"}],
            ["AWS/SQS", "ApproximateNumberOfMessagesNotVisible", "QueueName", queue, {"label": "in flight"}],
            ["ECS/ContainerInsights", "DesiredTaskCount", "ClusterName", cluster, "ServiceName", service,
             {"label": "desired tasks", "yAxis": "right"}],
            ["ECS/ContainerInsights", "RunningTaskCount", "ClusterName", cluster, "ServiceName", service,
             {"label": "running tasks", "yAxis": "right"}],
        ], right="tasks"),
        widget(16, 0, 8, 7, "Dead-letter queue", [
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", dlq, {"label": "failed images"}]]),
        widget(0, 7, 8, 6, "Worker CPU %", [
            ["AWS/ECS", "CPUUtilization", "ClusterName", cluster, "ServiceName", service]], stat="Average"),
        widget(8, 7, 8, 6, "Messages processed / min", [
            ["AWS/SQS", "NumberOfMessagesDeleted", "QueueName", queue, {"label": "classified"}],
            ["AWS/SQS", "NumberOfMessagesSent", "QueueName", queue, {"label": "uploaded"}]], stat="Sum"),
    ]
    if dist:
        widgets.append(widget(16, 7, 8, 6, "CDN (thumbnails)", [
            ["AWS/CloudFront", "Requests", "DistributionId", dist, "Region", "Global", {"label": "requests"}],
            ["AWS/CloudFront", "BytesDownloaded", "DistributionId", dist, "Region", "Global",
             {"label": "bytes", "yAxis": "right"}]], region="us-east-1", stat="Sum", right="bytes"))

    name = f"{sid}-strobe-a3"
    s.client("cloudwatch").put_dashboard(DashboardName=name, DashboardBody=json.dumps({"widgets": widgets}))
    print(f"dashboard {name}:")
    print(f"  https://{REGION}.console.aws.amazon.com/cloudwatch/home?region={REGION}#dashboards:name={name}")


if __name__ == "__main__":
    main()
