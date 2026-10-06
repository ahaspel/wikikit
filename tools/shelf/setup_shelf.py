"""Set up the reference shelf's AWS resources (tools/shelf/shelf.json).

    uv run --with boto3 python tools/shelf/setup_shelf.py           # DRY RUN: say what it would do
    uv run --with boto3 python tools/shelf/setup_shelf.py --apply   # do it

The shelf is ONE host (reference.aaronhaspel.com) serving several books, each
under its own prefix (/dnb/, /century/, ...): one S3 bucket, one CloudFront
distribution, one certificate, one DNS name.  Search goes to the Meilisearch host
the Britannica already runs (an index per book), through an allow-list at the
edge.  Modelled on britannica11.org's distribution (E24BJKH0IB4I6), which is only
READ here — nothing of the Britannica's is changed.

IDEMPOTENT: every step looks first and skips what exists, so a run that stops
(certificate validation can take minutes) is simply run again.  boto3, never the
installed `aws` CLI: that CLI is too old to see OriginAccessControlId and once
stripped it from a live distribution (britannica11.org, 2026-08-22).

Steps, in order:
  1. S3 bucket, private (public access blocked), versioning off.
  2. ACM certificate for the host (us-east-1, DNS-validated in the Route 53 zone).
  3. Origin access control for the bucket.
  4. CloudFront functions `shelf-router` and `shelf-search`, published LIVE.
  5. The distribution: S3 origin (behind the OAC) with `shelf-router`;
     /*/search-api/* to Meilisearch with `shelf-search`.  Same managed cache and
     origin-request policies as britannica11.org.
  6. Bucket policy: reads only by CloudFront, only for this distribution.
  7. Route 53: the host's A and AAAA alias records to the distribution.

Search keys are NOT made here: they are derived from the master key, which lives
only on the search host.  The run ends by printing that step for the host.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import boto3

HERE = Path(__file__).resolve().parent
SHELF = json.loads((HERE / "shelf.json").read_text(encoding="utf-8"))

#: The managed policies britannica11.org uses (read from its distribution).
CACHING_OPTIMIZED = "658327ea-f89d-4fab-a63d-7e88639e58f6"
CACHING_DISABLED = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
ALL_VIEWER_EXCEPT_HOST = "b689b0a8-53d0-40ab-baf2-68738e2966ac"
CLOUDFRONT_ZONE = "Z2FDTNDATAQYW2"     # every CloudFront distribution's alias zone

HOST, BUCKET, REGION = SHELF["host"], SHELF["bucket"], SHELF["region"]
S3_ORIGIN, SEARCH_ORIGIN = f"{BUCKET}.s3.{REGION}.amazonaws.com", "meilisearch"


class Plan:
    def __init__(self, apply: bool):
        self.apply = apply

    def step(self, what: str) -> bool:
        """Announce a change; True when it should be made."""
        print(("  DOING  " if self.apply else "  WOULD  ") + what)
        return self.apply


def bucket(plan, s3):
    names = {b["Name"] for b in s3.list_buckets()["Buckets"]}
    if BUCKET in names:
        print(f"  exists  bucket {BUCKET}")
        return
    if plan.step(f"create bucket {BUCKET} ({REGION}), all public access blocked"):
        s3.create_bucket(Bucket=BUCKET)
        s3.put_public_access_block(Bucket=BUCKET, PublicAccessBlockConfiguration={
            "BlockPublicAcls": True, "IgnorePublicAcls": True,
            "BlockPublicPolicy": True, "RestrictPublicBuckets": True})


def zone_id(r53) -> str:
    for z in r53.list_hosted_zones()["HostedZones"]:
        if z["Name"].rstrip(".") == SHELF["zone"]:
            return z["Id"].split("/")[-1]
    sys.exit(f"no Route 53 zone {SHELF['zone']}")


def certificate(plan, acm, r53, zone) -> str | None:
    for c in acm.list_certificates()["CertificateSummaryList"]:
        if c["DomainName"] == HOST:
            arn = c["CertificateArn"]
            status = acm.describe_certificate(CertificateArn=arn)["Certificate"]["Status"]
            print(f"  exists  certificate {HOST}: {status}")
            if status != "ISSUED":
                validation_record(plan, acm, r53, zone, arn)
            return arn if status == "ISSUED" else None
    if not plan.step(f"request certificate for {HOST} (DNS validation)"):
        return None
    arn = acm.request_certificate(DomainName=HOST, ValidationMethod="DNS")["CertificateArn"]
    validation_record(plan, acm, r53, zone, arn)
    return None


def validation_record(plan, acm, r53, zone, arn):
    for _ in range(30):                       # the record appears a few seconds after the request
        opts = acm.describe_certificate(CertificateArn=arn)["Certificate"].get("DomainValidationOptions", [])
        rr = opts[0].get("ResourceRecord") if opts else None
        if rr:
            break
        time.sleep(2)
    else:
        sys.exit("ACM gave no validation record yet; run again")
    if plan.step(f"add validation CNAME {rr['Name']} in {SHELF['zone']}"):
        r53.change_resource_record_sets(HostedZoneId=zone, ChangeBatch={"Changes": [{
            "Action": "UPSERT", "ResourceRecordSet": {
                "Name": rr["Name"], "Type": rr["Type"], "TTL": 300,
                "ResourceRecords": [{"Value": rr["Value"]}]}}]})
        print("          the certificate issues once DNS sees it (minutes); then run this again")


def oac(plan, cf) -> str | None:
    name = f"oac-{BUCKET}"
    for o in cf.list_origin_access_controls()["OriginAccessControlList"].get("Items", []):
        if o["Name"] == name:
            print(f"  exists  origin access control {o['Id']}")
            return o["Id"]
    if not plan.step(f"create origin access control {name}"):
        return None
    return cf.create_origin_access_control(OriginAccessControlConfig={
        "Name": name, "Description": f"CloudFront reads {BUCKET}",
        "SigningProtocol": "sigv4", "SigningBehavior": "always",
        "OriginAccessControlOriginType": "s3"})["OriginAccessControl"]["Id"]


def function(plan, cf, name: str, code: str) -> str | None:
    existing = {f["Name"]: f for f in cf.list_functions()["FunctionList"].get("Items", [])}
    if name in existing:
        desc = cf.describe_function(Name=name, Stage="DEVELOPMENT")
        live_code = cf.get_function(Name=name, Stage="DEVELOPMENT")["FunctionCode"].read().decode()
        if live_code == code:
            print(f"  exists  function {name} (code current)")
            return cf.describe_function(Name=name, Stage="LIVE")["FunctionSummary"]["FunctionMetadata"]["FunctionARN"]
        if not plan.step(f"update function {name} and publish it LIVE"):
            return None
        etag = cf.update_function(Name=name, IfMatch=desc["ETag"], FunctionCode=code.encode(),
                                  FunctionConfig={"Comment": name, "Runtime": "cloudfront-js-2.0"})["ETag"]
    else:
        if not plan.step(f"create function {name} and publish it LIVE"):
            return None
        etag = cf.create_function(Name=name, FunctionCode=code.encode(),
                                  FunctionConfig={"Comment": name, "Runtime": "cloudfront-js-2.0"})["ETag"]
    return cf.publish_function(Name=name, IfMatch=etag)["FunctionSummary"]["FunctionMetadata"]["FunctionARN"]


def distribution(plan, cf, cert, oac_id, router, search) -> dict | None:
    for d in cf.list_distributions().get("DistributionList", {}).get("Items", []):
        if HOST in d.get("Aliases", {}).get("Items", []):
            print(f"  exists  distribution {d['Id']} ({d['DomainName']}) {d['Status']}")
            return d
    if None in (cert, oac_id, router, search):
        print("  waits   distribution: needs the certificate issued and the OAC and functions made")
        return None
    if not plan.step(f"create distribution for {HOST}"):
        return None
    config = {
        "CallerReference": f"shelf-{int(time.time())}",
        "Comment": f"reference shelf: {HOST}",
        "Enabled": True,
        "Aliases": {"Quantity": 1, "Items": [HOST]},
        "DefaultRootObject": "index.html",
        "HttpVersion": "http2and3",
        "PriceClass": "PriceClass_100",
        "ViewerCertificate": {"ACMCertificateArn": cert, "SSLSupportMethod": "sni-only",
                              "MinimumProtocolVersion": "TLSv1.2_2021"},
        "Origins": {"Quantity": 2, "Items": [
            {"Id": S3_ORIGIN, "DomainName": S3_ORIGIN, "OriginPath": "",
             "S3OriginConfig": {"OriginAccessIdentity": ""}, "OriginAccessControlId": oac_id},
            {"Id": SEARCH_ORIGIN, "DomainName": SHELF["search_origin"], "OriginPath": "",
             "CustomOriginConfig": {"HTTPPort": SHELF["search_port"], "HTTPSPort": 443,
                                    "OriginProtocolPolicy": "http-only",
                                    "OriginSslProtocols": {"Quantity": 1, "Items": ["TLSv1.2"]},
                                    "OriginReadTimeout": 30, "OriginKeepaliveTimeout": 5}}]},
        "DefaultCacheBehavior": {
            "TargetOriginId": S3_ORIGIN, "ViewerProtocolPolicy": "redirect-to-https",
            "CachePolicyId": CACHING_OPTIMIZED, "Compress": True,
            "AllowedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"],
                               "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]}},
            "FunctionAssociations": {"Quantity": 1, "Items": [
                {"FunctionARN": router, "EventType": "viewer-request"}]}},
        "CacheBehaviors": {"Quantity": 1, "Items": [{
            "PathPattern": "/*/search-api/*", "TargetOriginId": SEARCH_ORIGIN,
            "ViewerProtocolPolicy": "redirect-to-https", "Compress": True,
            "CachePolicyId": CACHING_DISABLED, "OriginRequestPolicyId": ALL_VIEWER_EXCEPT_HOST,
            "AllowedMethods": {"Quantity": 7, "Items": ["HEAD", "DELETE", "POST", "GET", "OPTIONS", "PUT", "PATCH"],
                               "CachedMethods": {"Quantity": 2, "Items": ["HEAD", "GET"]}},
            "FunctionAssociations": {"Quantity": 1, "Items": [
                {"FunctionARN": search, "EventType": "viewer-request"}]}}]},
    }
    d = cf.create_distribution(DistributionConfig=config)["Distribution"]
    return {"Id": d["Id"], "DomainName": d["DomainName"], "ARN": d["ARN"], "Status": d["Status"]}


def bucket_policy(plan, s3, dist):
    arn = dist.get("ARN") or f"arn:aws:cloudfront::{boto3.client('sts').get_caller_identity()['Account']}:distribution/{dist['Id']}"
    policy = {"Version": "2012-10-17", "Statement": [{
        "Sid": "CloudFrontReadsTheShelf", "Effect": "Allow",
        "Principal": {"Service": "cloudfront.amazonaws.com"},
        "Action": "s3:GetObject", "Resource": f"arn:aws:s3:::{BUCKET}/*",
        "Condition": {"StringEquals": {"AWS:SourceArn": arn}}}]}
    try:
        if json.loads(s3.get_bucket_policy(Bucket=BUCKET)["Policy"]) == policy:
            print("  exists  bucket policy (CloudFront only)")
            return
    except s3.exceptions.from_code("NoSuchBucketPolicy"):
        pass
    if plan.step("set bucket policy: reads only by this distribution"):
        s3.put_bucket_policy(Bucket=BUCKET, Policy=json.dumps(policy))


def dns(plan, r53, zone, dist):
    have = {(r["Name"].rstrip("."), r["Type"]) for r in
            r53.list_resource_record_sets(HostedZoneId=zone)["ResourceRecordSets"]}
    for kind in ("A", "AAAA"):
        if (HOST, kind) in have:
            print(f"  exists  {kind} {HOST}")
            continue
        if plan.step(f"add {kind} alias {HOST} -> {dist['DomainName']}"):
            r53.change_resource_record_sets(HostedZoneId=zone, ChangeBatch={"Changes": [{
                "Action": "CREATE", "ResourceRecordSet": {
                    "Name": HOST, "Type": kind, "AliasTarget": {
                        "HostedZoneId": CLOUDFRONT_ZONE, "DNSName": dist["DomainName"],
                        "EvaluateTargetHealth": False}}}]})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="make the changes (default: only say them)")
    plan = Plan(ap.parse_args().apply)
    print(f"reference shelf {HOST} — {'APPLYING' if plan.apply else 'DRY RUN'}")
    s3, r53, cf = boto3.client("s3", region_name=REGION), boto3.client("route53"), boto3.client("cloudfront")
    acm = boto3.client("acm", region_name="us-east-1")
    zone = zone_id(r53)
    bucket(plan, s3)
    cert = certificate(plan, acm, r53, zone)
    oac_id = oac(plan, cf)
    router = function(plan, cf, "shelf-router", (HERE / "shelf-router.js").read_text(encoding="utf-8"))
    search = function(plan, cf, "shelf-search", (HERE / "shelf-search.js").read_text(encoding="utf-8")
                      .replace("__INDEXES__", json.dumps(SHELF["books"])))
    dist = distribution(plan, cf, cert, oac_id, router, search)
    if dist:
        bucket_policy(plan, s3, dist)
        dns(plan, r53, zone, dist)
    print("\nOn the search host (once): a search-only key per book index, derived from the")
    print("master key that lives only there — e.g. for the DNB:")
    print("  curl -s -X POST http://localhost:7700/keys -H \"Authorization: Bearer $(cat ~/.meili_master_key)\" \\")
    print("    -H 'Content-Type: application/json' \\")
    print("    -d '{\"name\":\"dnb-search\",\"actions\":[\"search\"],\"indexes\":[\"dnb\"],\"expiresAt\":null}'")
    print("The key it returns is public by design: it goes in the book's profile (search_key).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
