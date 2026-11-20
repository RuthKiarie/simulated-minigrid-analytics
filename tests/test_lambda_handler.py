"""Test the Lambda handler against mocked S3, SNS and SQS (no real AWS)."""

import json

import boto3
from moto import mock_aws

from data.generate_data import generate
from src import anomaly_lambda
from src.schemas import validate_dataframe


def _aws_env(monkeypatch):
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(key, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


def test_parse_event_supports_s3_notifications_and_direct_calls():
    s3_event = {
        "Records": [{"s3": {"bucket": {"name": "b"}, "object": {"key": "curated/x.csv"}}}]
    }
    assert anomaly_lambda._parse_event(s3_event) == ("b", "curated/x.csv")
    assert anomaly_lambda._parse_event({"bucket": "b", "key": "k"}) == ("b", "k")


@mock_aws
def test_handler_reads_s3_and_publishes_sns_summary(tmp_path, monkeypatch):
    _aws_env(monkeypatch)
    valid, _ = validate_dataframe(
        generate(out_dir=str(tmp_path), seed=7, offline=True)["telemetry"]
    )
    s3 = boto3.client("s3")
    s3.create_bucket(Bucket="data-bucket")
    s3.put_object(
        Bucket="data-bucket",
        Key="curated/telemetry.csv",
        Body=valid.to_csv(index=False).encode(),
    )
    sns, sqs = boto3.client("sns"), boto3.client("sqs")
    topic = sns.create_topic(Name="alerts")["TopicArn"]
    queue = sqs.create_queue(QueueName="inbox")["QueueUrl"]
    queue_arn = sqs.get_queue_attributes(
        QueueUrl=queue, AttributeNames=["QueueArn"]
    )["Attributes"]["QueueArn"]
    sns.subscribe(TopicArn=topic, Protocol="sqs", Endpoint=queue_arn)
    monkeypatch.setenv("SNS_TOPIC_ARN", topic)
    event = {
        "Records": [
            {"s3": {"bucket": {"name": "data-bucket"}, "object": {"key": "curated/telemetry.csv"}}}
        ]
    }
    result = anomaly_lambda.lambda_handler(event)
    assert result["alerts"] > 0
    messages = sqs.receive_message(QueueUrl=queue, MaxNumberOfMessages=1)["Messages"]
    summary = json.loads(json.loads(messages[0]["Body"])["Message"])
    assert summary["file"] == "s3://data-bucket/curated/telemetry.csv"
    assert {"silent_meter", "distribution_loss", "voltage_sag", "diesel_spike"} <= set(
        summary["counts"]
    )


@mock_aws
def test_handler_without_topic_still_returns_alert_count(tmp_path, monkeypatch):
    _aws_env(monkeypatch)
    monkeypatch.delenv("SNS_TOPIC_ARN", raising=False)
    valid, _ = validate_dataframe(
        generate(out_dir=str(tmp_path), seed=7, offline=True)["telemetry"]
    )
    s3 = boto3.client("s3")
    s3.create_bucket(Bucket="data-bucket")
    s3.put_object(
        Bucket="data-bucket",
        Key="curated/t.csv",
        Body=valid.to_csv(index=False).encode(),
    )
    result = anomaly_lambda.lambda_handler({"bucket": "data-bucket", "key": "curated/t.csv"})
    assert result["alerts"] > 0
