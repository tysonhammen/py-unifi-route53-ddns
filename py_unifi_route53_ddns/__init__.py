import argparse
import getpass
import ipaddress
import logging
import os
import re
import shutil
import subprocess
import sys
from collections import Counter

import boto3
import urllib3

systemd_service = """[Unit]
Description="py-unifi-route53-ddns"

[Service]
ExecStart={python} {wrapper} run
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


def _coerce_ipv4(s):
    if not s:
        return None
    try:
        addr = ipaddress.ip_address(s.strip())
    except ValueError:
        return None
    if not isinstance(addr, ipaddress.IPv4Address):
        return None
    if addr.is_loopback or addr.is_unspecified:
        return None
    return str(addr)


def _ip_from_cloudflare_trace(body):
    for line in body.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "ip":
            return _coerce_ipv4(value)
    return None


def _probe_public_ip(url, kind):
    res = http.request("GET", url, timeout=15.0)
    if res.status != 200:
        logger.warning("Probe %s returned HTTP %s", url, res.status)
        return None
    text = res.data.decode()
    if kind == "plain":
        return _coerce_ipv4(text)
    if kind == "trace":
        return _ip_from_cloudflare_trace(text)
    raise ValueError(kind)


_IFNAME_RE = re.compile(r"^[A-Za-z0-9._@-]+$")


def _ipv4_from_linux_interface(ifname):
    """Return first usable IPv4 on ifname from `ip -4 addr show` (global preferred)."""
    if not ifname or not _IFNAME_RE.fullmatch(ifname):
        raise ValueError(f"Invalid interface name: {ifname!r}")
    ip_bin = shutil.which("ip") or "/sbin/ip"
    res = subprocess.run(
        [ip_bin, "-4", "addr", "show", "dev", ifname],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if res.returncode != 0:
        logger.warning(
            "`%s -4 addr show dev %s` failed (%s): %s",
            ip_bin,
            ifname,
            res.returncode,
            (res.stderr or "").strip(),
        )
        return None
    global_candidates = []
    other_candidates = []
    for line in res.stdout.splitlines():
        line = line.strip()
        if not line.startswith("inet "):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        host = parts[1].split("/")[0]
        parsed = _coerce_ipv4(host)
        if not parsed:
            continue
        if "scope" in parts and "global" in parts:
            global_candidates.append(parsed)
        else:
            other_candidates.append(parsed)
    chosen = (global_candidates or other_candidates or [None])[0]
    return chosen


def _wan_interfaces_from_env():
    raw = (os.environ.get("ROUTE53_WAN_INTERFACES") or "").strip()
    if raw:
        return [p.strip() for p in raw.split(",") if p.strip()]
    one = (os.environ.get("ROUTE53_WAN_INTERFACE") or "").strip()
    return [one] if one else []


def get_my_ip():
    wan_ifaces = _wan_interfaces_from_env()
    for ifname in wan_ifaces:
        ip = _ipv4_from_linux_interface(ifname)
        if ip:
            logger.info("Using IPv4 %s from Linux interface %s (WAN interface mode)", ip, ifname)
            return ip
        logger.warning("No usable IPv4 on interface %s", ifname)

    if wan_ifaces:
        raise RuntimeError(
            "ROUTE53_WAN_INTERFACE(s) set but no IPv4 found on those interfaces; check names (e.g. eth8, eth9)"
        )

    override = (os.environ.get("ROUTE53_PUBLIC_IP_URL") or "").strip()
    if override:
        ip = _probe_public_ip(override, "plain")
        if not ip:
            raise RuntimeError(f"ROUTE53_PUBLIC_IP_URL ({override!r}) did not return a usable IPv4 address")
        logger.info("Using public IPv4 %s from ROUTE53_PUBLIC_IP_URL", ip)
        return ip

    probes = (
        ("https://checkip.amazonaws.com", "plain"),
        ("https://api.ipify.org", "plain"),
        ("https://ipv4.icanhazip.com", "plain"),
        ("https://cloudflare.com/cdn-cgi/trace", "trace"),
    )
    results = []
    for url, kind in probes:
        try:
            ip = _probe_public_ip(url, kind)
        except Exception as e:
            logger.warning("Probe %s failed: %s", url, e)
            continue
        if ip:
            results.append((url, ip))

    if not results:
        raise RuntimeError("Could not resolve a public IPv4 address from any built-in probe")

    counts = Counter(ip for _, ip in results)
    top_ip, top_n = counts.most_common(1)[0]
    if top_n >= 2:
        logger.info("Resolved public IPv4 as %s (%d of %d probes agreed)", top_ip, top_n, len(results))
        return top_ip

    chosen_ip, chosen_url = results[0][1], results[0][0]
    if len(results) == 1:
        logger.info("Resolved public IPv4 as %s (only %s responded)", chosen_ip, chosen_url)
    else:
        logger.warning(
            "Public IP probes disagreed: %s; using %s from %s (first successful probe)",
            dict(counts),
            chosen_ip,
            chosen_url,
        )
    return chosen_ip


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
    wrapper = shutil.which("py-unifi-route53-ddns")
    if not wrapper:
        parser.exit("unable to resolve location of py-unifi-route53-ddns")
    logger.info("Installing /etc/systemd/system/py-unifi-route53-ddns.service...")
    with open("/etc/systemd/system/py-unifi-route53-ddns.service", "w") as service_fh:
        # Run the venv Python with the console script as an argument so systemd never
        # exec()s the wrapper directly (203/EXEC if the shebang interpreter is wrong)
        # and we do not require py_unifi_route53_ddns.__main__ in site-packages.
        service_fh.write(systemd_service.format(python=sys.executable, wrapper=wrapper))
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
