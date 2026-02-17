import argparse
import getpass
import logging
import os
import shutil

import boto3
import urllib3

systemd_service = """[Unit]
Description="py-unifi-route53-ddns"

[Service]
ExecStart={entrypoint} run
"""

systemd_timer = """[Unit]
Description="Run py-unifi-route53-ddns.service every 5 minutes"

[Timer]
OnCalendar=*:5/10
Unit=py-unifi-route53-ddns.service

[Install]
WantedBy=multi-user.target
"""

systemd_override = """[Service]
Environment="AWS_ACCESS_KEY_ID={akid}"
Environment="AWS_SECRET_ACCESS_KEY={access_key}"
Environment="ROUTE53_HOSTED_ZONE_DNS_NAME={zone_name}"
Environment="ROUTE53_MY_DNS_NAMES={host_names}"
Environment="ROUTE53_TTL=300"
"""

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
route53 = boto3.client("route53")
http = urllib3.PoolManager()
parser = argparse.ArgumentParser(prog=__name__)
parser.add_argument("action", choices=["install", "run"])


def get_my_ip():
    res = http.request("GET", "https://cloudflare.com/cdn-cgi/trace")
    for line in res.data.decode().splitlines():
        data = line.split("=")
        if data[0] == "ip":
            return data[1]


def get_hosted_zone_id(hosted_zone_dns_name):
    res = route53.list_hosted_zones_by_name(DNSName=hosted_zone_dns_name)
    return res["HostedZones"][0]["Id"]


def get_route53_ip(hosted_zone_id, my_dns_name):
    lrrs_paginator = route53.get_paginator("list_resource_record_sets")
    for page in lrrs_paginator.paginate(HostedZoneId=hosted_zone_id):
        for rrs in page["ResourceRecordSets"]:
            if rrs["Name"] == f"{my_dns_name}." and rrs["Type"] == "A":
                return rrs["ResourceRecords"][0]["Value"]
    return None


def set_route53_ip(new_ip, my_dns_name, hosted_zone_id, ttl):
    route53_change = {
        "Action": "UPSERT",
        "ResourceRecordSet": {
            "Name": f"{my_dns_name}.",
            "Type": "A",
            "ResourceRecords": [{"Value": new_ip}],
            "TTL": ttl,
        },
    }
    res = route53.change_resource_record_sets(HostedZoneId=hosted_zone_id, ChangeBatch={"Changes": [route53_change]})
    logger.info("Completed update: %s", res)


def _get_dns_names():
    """Return list of DNS names from ROUTE53_MY_DNS_NAMES (comma-separated) or legacy ROUTE53_MY_DNS_NAME."""
    names = os.environ.get("ROUTE53_MY_DNS_NAMES") or os.environ.get("ROUTE53_MY_DNS_NAME")
    if not names:
        raise KeyError("Set ROUTE53_MY_DNS_NAMES or ROUTE53_MY_DNS_NAME")
    return [n.strip() for n in names.split(",") if n.strip()]


def run():
    HOSTED_ZONE_DNS_NAME = os.environ["ROUTE53_HOSTED_ZONE_DNS_NAME"]
    dns_names = _get_dns_names()
    TTL = int(os.environ["ROUTE53_TTL"])
    my_ip = get_my_ip()
    hosted_zone_id = get_hosted_zone_id(HOSTED_ZONE_DNS_NAME)
    for my_dns_name in dns_names:
        route53_ip = get_route53_ip(hosted_zone_id=hosted_zone_id, my_dns_name=my_dns_name)
        if my_ip != route53_ip:
            logger.info(
                "Will update IP in %s (%s) for %s from %s to %s",
                HOSTED_ZONE_DNS_NAME,
                hosted_zone_id,
                my_dns_name,
                route53_ip,
                my_ip,
            )
            set_route53_ip(new_ip=my_ip, my_dns_name=my_dns_name, hosted_zone_id=hosted_zone_id, ttl=TTL)
        else:
            logger.info(
                "IP in %s (%s) for %s (%s) matches, nothing to do",
                HOSTED_ZONE_DNS_NAME,
                hosted_zone_id,
                my_dns_name,
                my_ip,
            )


def install():
    if not shutil.which("systemctl"):
        parser.exit("systemctl does not appear to be active")
    if not shutil.which("py-unifi-route53-ddns"):
        parser.exit("unable to resolve location of py-unifi-route53-ddns")
    logger.info("Installing /etc/systemd/system/py-unifi-route53-ddns.service...")
    with open("/etc/systemd/system/py-unifi-route53-ddns.service", "w") as service_fh:
        service_fh.write(systemd_service.format(entrypoint=shutil.which("py-unifi-route53-ddns")))
    logger.info("Installing /etc/systemd/system/py-unifi-route53-ddns.timer...")
    with open("/etc/systemd/system/py-unifi-route53-ddns.timer", "w") as timer_fh:
        timer_fh.write(systemd_timer)
    os.makedirs("/etc/systemd/system/py-unifi-route53-ddns.service.d", exist_ok=True)
    akid = input("AWS access key ID: ")
    access_key = getpass.getpass("AWS secret access key (hidden): ")
    zone_name = input("Route53 hosted zone DNS name (e.g. example.net): ")
    host_names = input(
        "Route53 dynamic host name(s), comma-separated (e.g. unifi.example.net, camera.example.net): "
    ).strip()
    with open("/etc/systemd/system/py-unifi-route53-ddns.service.d/env.conf", "w") as env_fh:
        env_fh.write(
            systemd_override.format(
                akid=akid, access_key=access_key, zone_name=zone_name, host_names=host_names
            )
        )
    logger.info(
        'Done. Please run "systemctl start py-unifi-route53-ddns.timer" and "systemctl enable py-unifi-route53-ddns.timer".'
    )


def main():
    args = parser.parse_args()
    if args.action == "install":
        install()
    elif args.action == "run":
        run()
