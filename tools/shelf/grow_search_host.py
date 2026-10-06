"""Grow the search host (the britannica11.org Meilisearch machine) into the
shelf's search tier.  Three phases, each a DRY RUN unless --apply:

    uv run --with boto3 python tools/shelf/grow_search_host.py check            # read-only preflight
    uv run --with boto3 python tools/shelf/grow_search_host.py disk   [--apply] # no downtime
    uv run --with boto3 python tools/shelf/grow_search_host.py window [--apply] # ~10-15 min without search

disk    The root volume 8 -> DISK_GB, while running; the partition and the XFS
        filesystem then grown on the host (growpart, xfs_growfs).  No downtime.

window  Off-peak.  EB1911 search is down while it runs.
        1. Allocate an Elastic IP (tagged) and point SEARCH_NAME at it (Route 53).
           AWS cannot keep the machine's current automatic address: an Elastic
           IP is a new one.  From now on everything names SEARCH_NAME, so the
           address never matters again.
        2. Stop the instance; change its type to INSTANCE_TYPE; associate the
           Elastic IP; start it; wait until Meilisearch answers on the host
           (its container restarts with Docker — checked by `check`).
        3. Point britannica11.org's /search-api/* origin at SEARCH_NAME: ONE
           field of distribution E24BJKH0IB4I6, through boto3 (never the old
           aws CLI, which stripped the origin access control on 2026-08-22).
           The whole config is compared before and after: anything else
           different and it stops without updating.
        4. Wait for CloudFront to deploy; check https://britannica11.org/search-api/health.

After `window`, tools/deploy.sh must name SEARCH_NAME instead of the old
ec2-44-222-119-72 host (a change in britannica-edition, prepared separately).
Idempotent: a phase looks first and skips what is done.
"""
from __future__ import annotations

import argparse
import copy
import subprocess
import sys
import time
import urllib.request

import boto3

INSTANCE = "i-015870710fb438b85"
REGION = "us-east-1"
OLD_HOST = "ec2-44-222-119-72.compute-1.amazonaws.com"
SEARCH_NAME = "search.aaronhaspel.com"
ZONE = "aaronhaspel.com"
DISTRIBUTION = "E24BJKH0IB4I6"
SEARCH_ORIGIN_ID = "meilisearch"
DISK_GB = 50
INSTANCE_TYPE = "t3.large"     # 8 GB: EB1911, the DNB, the Century, and room (2026-10-04)
SSH_KEY = "D:/work/web/cloudinstall/britannica11.pem"

ec2 = boto3.client("ec2", region_name=REGION)
r53 = boto3.client("route53")
cf = boto3.client("cloudfront")


def say(apply: bool, what: str) -> bool:
    print(("  DOING  " if apply else "  WOULD  ") + what)
    return apply


def instance() -> dict:
    return ec2.describe_instances(InstanceIds=[INSTANCE])["Reservations"][0]["Instances"][0]


def ssh(host: str, command: str) -> str:
    out = subprocess.run(["ssh", "-o", "ConnectTimeout=15", "-o", "BatchMode=yes",
                          "-o", "StrictHostKeyChecking=accept-new", "-i", SSH_KEY,
                          f"ec2-user@{host}", command], capture_output=True, text=True)
    return (out.stdout + out.stderr).strip()


def host_name() -> str:
    """Where the machine answers now: its own public DNS name."""
    return instance()["PublicDnsName"]


def check(_apply: bool) -> None:
    i = instance()
    vol = ec2.describe_volumes(VolumeIds=[b["Ebs"]["VolumeId"] for b in i["BlockDeviceMappings"]])["Volumes"][0]
    eip = [a for a in ec2.describe_addresses()["Addresses"] if a.get("InstanceId") == INSTANCE]
    print(f"  instance {INSTANCE}: {i['InstanceType']} {i['State']['Name']}, {i.get('PublicIpAddress')}")
    print(f"  root volume {vol['VolumeId']}: {vol['Size']} GiB {vol['VolumeType']}")
    print(f"  elastic IP: {[a['PublicIp'] for a in eip] or 'none'}")
    print("  on the host:")
    print("   ", ssh(host_name(), "systemctl is-enabled docker; "
               "sudo docker inspect -f '{{.Name}} restart={{.HostConfig.RestartPolicy.Name}}' meilisearch; "
               "lsblk -no NAME,SIZE /dev/nvme0n1; df -hT / | tail -1").replace("\n", "\n    "))
    origin = next(o for o in cf.get_distribution_config(Id=DISTRIBUTION)["DistributionConfig"]["Origins"]["Items"]
                  if o["Id"] == SEARCH_ORIGIN_ID)
    print(f"  britannica11 search origin: {origin['DomainName']}:{origin['CustomOriginConfig']['HTTPPort']}")


def disk(apply: bool) -> None:
    i = instance()
    vol_id = i["BlockDeviceMappings"][0]["Ebs"]["VolumeId"]
    vol = ec2.describe_volumes(VolumeIds=[vol_id])["Volumes"][0]
    if vol["Size"] >= DISK_GB:
        print(f"  done    volume already {vol['Size']} GiB")
    elif say(apply, f"grow volume {vol_id} {vol['Size']} -> {DISK_GB} GiB (online)"):
        ec2.modify_volume(VolumeId=vol_id, Size=DISK_GB)
        while ec2.describe_volumes_modifications(VolumeIds=[vol_id])["VolumesModifications"][0][
                "ModificationState"] == "modifying":
            time.sleep(10)
    if say(apply, "grow partition 1 and the XFS filesystem on / to fill the volume"):
        print("   ", ssh(host_name(), "sudo growpart /dev/nvme0n1 1; sudo xfs_growfs -d /; df -h / | tail -1")
              .replace("\n", "\n    "))


def window(apply: bool) -> None:
    # 1. the Elastic IP and the name
    eips = [a for a in ec2.describe_addresses()["Addresses"]
            if any(t["Key"] == "Name" and t["Value"] == SEARCH_NAME for t in a.get("Tags", []))]
    if eips:
        eip = eips[0]
        print(f"  exists  elastic IP {eip['PublicIp']}")
    elif say(apply, f"allocate an elastic IP tagged {SEARCH_NAME}"):
        eip = ec2.allocate_address(Domain="vpc", TagSpecifications=[{
            "ResourceType": "elastic-ip", "Tags": [{"Key": "Name", "Value": SEARCH_NAME}]}])
    else:
        eip = {"PublicIp": "<new elastic IP>", "AllocationId": None}
    zone = next(z["Id"] for z in r53.list_hosted_zones()["HostedZones"] if z["Name"].rstrip(".") == ZONE)
    if say(apply, f"A record {SEARCH_NAME} -> {eip['PublicIp']} (TTL 300)"):
        r53.change_resource_record_sets(HostedZoneId=zone, ChangeBatch={"Changes": [{
            "Action": "UPSERT", "ResourceRecordSet": {"Name": SEARCH_NAME, "Type": "A", "TTL": 300,
                                                      "ResourceRecords": [{"Value": eip["PublicIp"]}]}}]})

    # 2. stop, resize, associate, start
    i = instance()
    needs_type = i["InstanceType"] != INSTANCE_TYPE
    needs_eip = i.get("PublicIpAddress") != eip["PublicIp"]
    if needs_type or needs_eip:
        # Docker was found DISABLED at boot (2026-10-04): the container's
        # `unless-stopped` restarts it only once Docker runs, so a stop/start
        # would leave search down.  Enable it first, and stop only if it took.
        if say(apply, "enable Docker at boot on the host (systemctl enable docker)"):
            state = ssh(host_name(), "sudo systemctl enable docker >/dev/null 2>&1; systemctl is-enabled docker")
            if state.strip().splitlines()[-1] != "enabled":
                sys.exit(f"Docker is not enabled at boot ({state!r}): not stopping the machine")
        if say(apply, f"stop {INSTANCE} (EB1911 search goes down here)"):
            ec2.stop_instances(InstanceIds=[INSTANCE])
            ec2.get_waiter("instance_stopped").wait(InstanceIds=[INSTANCE])
        if needs_type and say(apply, f"change type {i['InstanceType']} -> {INSTANCE_TYPE}"):
            ec2.modify_instance_attribute(InstanceId=INSTANCE, InstanceType={"Value": INSTANCE_TYPE})
        if needs_eip and say(apply, f"associate elastic IP {eip['PublicIp']}"):
            ec2.associate_address(InstanceId=INSTANCE, AllocationId=eip["AllocationId"])
        if say(apply, f"start {INSTANCE} and wait for Meilisearch"):
            ec2.start_instances(InstanceIds=[INSTANCE])
            ec2.get_waiter("instance_running").wait(InstanceIds=[INSTANCE])
            for _ in range(40):
                if '"available"' in ssh(eip["PublicIp"], "curl -s http://localhost:7700/health"):
                    print("          Meilisearch is up on the host")
                    break
                time.sleep(15)
            else:
                sys.exit("Meilisearch did not come up: look on the host before going on")
    else:
        print(f"  done    {INSTANCE_TYPE} with {eip['PublicIp']}")

    # 3. britannica11's search origin -> the name, one field, verified
    got = cf.get_distribution_config(Id=DISTRIBUTION)
    before, etag = got["DistributionConfig"], got["ETag"]
    after = copy.deepcopy(before)
    origin = next(o for o in after["Origins"]["Items"] if o["Id"] == SEARCH_ORIGIN_ID)
    if origin["DomainName"] == SEARCH_NAME:
        print(f"  done    britannica11 search origin is {SEARCH_NAME}")
    else:
        origin["DomainName"] = SEARCH_NAME
        changed = [k for k in before if before[k] != after[k]]
        s3 = [o.get("OriginAccessControlId") for o in after["Origins"]["Items"] if "S3OriginConfig" in o]
        if changed != ["Origins"] or not all(s3):
            sys.exit(f"refusing: the update would change {changed} / S3 OAC {s3}")
        if say(apply, f"britannica11 search origin {OLD_HOST} -> {SEARCH_NAME} (S3 OAC kept: {s3})"):
            cf.update_distribution(Id=DISTRIBUTION, IfMatch=etag, DistributionConfig=after)
            cf.get_waiter("distribution_deployed").wait(Id=DISTRIBUTION)

    # 4. the live check
    if apply:
        body = urllib.request.urlopen("https://britannica11.org/search-api/health", timeout=20).read().decode()
        print(f"  live    https://britannica11.org/search-api/health -> {body}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["check", "disk", "window"])
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    print(f"search host {INSTANCE} — {a.phase} — {'APPLYING' if a.apply else 'DRY RUN'}")
    {"check": check, "disk": disk, "window": window}[a.phase](a.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
