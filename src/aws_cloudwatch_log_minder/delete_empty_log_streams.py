import json
from datetime import datetime, timedelta
from typing import List

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from .logger import log

cw_logs = boto3.client("logs", config=Config(retries=dict(max_attempts=10)))


def ms_to_datetime(ms: int) -> datetime:
    return datetime(1970, 1, 1) + timedelta(milliseconds=ms)


def _delete_empty_log_streams(group: dict, purge_non_empty: bool = False, dry_run: bool = False):
    now = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    log_group_name = group["logGroupName"]
    retention_in_days = group.get("retentionInDays", 0)
    if not retention_in_days:
        log.info(
            "skipping log group %s as it has no retention period set", log_group_name
        )
        return

    log.info(
        "deleting streams from log group %s older than the retention period of %s days",
        log_group_name,
        retention_in_days,
    )

    kwargs = {
        "logGroupName": log_group_name,
        "orderBy": "LastEventTime",
        "descending": False,
        "PaginationConfig": {"PageSize": 50},
    }

    for response in cw_logs.get_paginator("describe_log_streams").paginate(**kwargs):
        for stream in response["logStreams"]:
            stored_bytes = stream["storedBytes"]
            log_stream_name = stream["logStreamName"]
            last_event = ms_to_datetime(
                stream.get("lastEventTimestamp", stream.get("creationTime"))
            )
            if last_event > (now - timedelta(days=retention_in_days)):
                log.info(
                    "there are no log streams from group %s older than the retention period of %s days",
                    log_group_name,
                    retention_in_days,
                )
                return

            if not purge_non_empty and stored_bytes:
                log.warn(
                    "keeping group %s, log stream %s, with %s bytes last event stored on %s",
                    log_group_name,
                    log_stream_name,
                    stream["storedBytes"],
                    last_event,
                )
                continue

            log.info(
                "deleting from group %s, log stream %s, with %s bytes last event stored on %s",
                log_group_name,
                log_stream_name,
                stream["storedBytes"],
                last_event,
            )
            if dry_run:
                continue

            try:
                cw_logs.delete_log_stream(
                    logGroupName=log_group_name, logStreamName=log_stream_name
                )
            except ClientError as e:
                log.error(
                    "failed to delete log stream %s from group %s, %s",
                    log_stream_name,
                    log_group_name,
                    e,
                )


def delete_empty_log_streams(log_group_name_prefix: str = None, purge_non_empty: bool = False, dry_run: bool = False):
    kwargs = {"PaginationConfig": {"PageSize": 50}}
    if log_group_name_prefix:
        kwargs["logGroupNamePrefix"] = log_group_name_prefix

    for response in cw_logs.get_paginator("describe_log_groups").paginate(**kwargs):
        for group in response["logGroups"]:
            _delete_empty_log_streams(group, purge_non_empty, dry_run)


def get_all_log_group_names() -> List[str]:
    result: List[str] = []
    for response in cw_logs.get_paginator("describe_log_groups").paginate(
            PaginationConfig={"PageSize": 50}
    ):
        result.extend(list(map(lambda g: g["logGroupName"], response["logGroups"])))
    return result


def fan_out(function_arn: str, log_group_names: List[str], purge_non_empty: bool, dry_run: bool):
    awslambda = boto3.client("lambda")
    log.info(
        "recursively invoking %s to delete empty log streams from %d log groups",
        function_arn,
        len(log_group_names),
    )
    for log_group_name in log_group_names:
        payload = json.dumps(
            {
                "log_group_name_prefix": log_group_name,
                "purge_non_empty": purge_non_empty,
                "dry_run": dry_run,
            })
        awslambda.invoke(
            FunctionName=function_arn, InvocationType="Event", Payload=payload
        )


def handle(request, context):
    dry_run = request.get("dry_run", False)
    if "dry_run" in request and not isinstance(dry_run, bool):
        raise ValueError(f"'dry_run' is not a boolean value, {request}")

    purge_non_empty = request.get("purge_non_empty", False)
    if "purge_non_empty" in request and not isinstance(dry_run, bool):
        raise ValueError(f"'purge_non_empty' is not a boolean value, {request}")

    log_group_name_prefix = request.get("log_group_name_prefix")
    if log_group_name_prefix:
        delete_empty_log_streams(log_group_name_prefix, purge_non_empty, dry_run)
    else:
        fan_out(context.invoked_function_arn, get_all_log_group_names(), purge_non_empty, dry_run)
