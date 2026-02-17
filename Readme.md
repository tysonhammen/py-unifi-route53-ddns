# `py-unifi-route53-ddns`
This is a minimalistic utility to run dynamic DNS updates on Ubiquiti UniFi Gateway consoles using AWS Route53 DNS.

[Ubiquiti UniFi gateways](https://store.ui.com/us/en?category=all-unifi-cloud-gateways) such as [UniFi Express](https://store.ui.com/us/en/category/all-unifi-cloud-gateways/products/ux), [Cloud Gateway Max](https://store.ui.com/us/en/category/cloud-gateways-compact/collections/cloud-gateway-max/products/ucg-max) and [Dream Machine SE](https://store.ui.com/us/en/category/cloud-gateways-large-scale/products/udm-se) provide Internet gateway router functions for home and small business networks. When running the network on an ISP connection without a reserved static IP, you can use dynamic DNS updating to bind the dynamically assigned IP address to a DNS name (such as home.example.net). This DNS name can then be used with a WireGuard configuration to VPN to the network, for example. While the UniFi router software has some built-in connectors to third-party dynamic DNS services, it does not integrate with AWS Route53, which is the DNS provider of choice for many people. Luckily, UniFi runs on Ubuntu and allows the console to be accessed via SSH, so we can configure this using standard Ubuntu tools.

`py-unifi-route53-ddns` uses the system Python on this Ubuntu OS to install a virtualenv to isolate its dependencies from the rest of the system, and installs a systemd timer and service (effectively a cron job) to update the DNS hostname in Route53 every 5 minutes.

### Installation
* Decide which domain name you will use to host your dynamic name, and configure a Route53 hosted zone for it if you
  haven't already.
* Create an AWS IAM user with the IAM permissions listed in the **IAM permissions** section below.
* Create an access key credential for the AWS IAM user and have it handy to copy into the terminal.
* Enable SSH in the UniFi console (navigate to Control Plane -> Console -> Advanced -> SSH) and set the password.
* Connect to the console via `ssh ui@192.168.1.1` and run the following commands:
```
apt install python3-distutils
python3 -m venv /usr/local/share/pyuir53ddns --without-pip
source /usr/local/share/pyuir53ddns/bin/activate
wget https://bootstrap.pypa.io/get-pip.py
python get-pip.py
pip install https://github.com/tysonhammen/py-unifi-route53-ddns/archive/refs/heads/main.zip
py-unifi-route53-ddns install
```
The install script will prompt you for your access key ID, access key, hosted zone domain name, and dynamic hostname(s) to update (comma-separated for multiple entries, e.g. `unifi.example.net, camera.example.net`). These variables will be saved to the systemd service override file in `/etc/systemd/system/py-unifi-route53-ddns.service.d/env.conf`. Other files created by the service are:

* `/etc/systemd/system/py-unifi-route53-ddns.service`
* `/etc/systemd/system/py-unifi-route53-ddns.timer`
* `/usr/local/share/pyuir53ddns`, the virtualenv, as seen above

To remove the service, just delete all of these files.

### Upgrading
If you see `TypeError: cannot unpack non-iterable NoneType object` (e.g. when the A record does not exist yet in Route53), you are running an older version. Reinstall inside the same virtualenv to get the latest code:

```
source /usr/local/share/pyuir53ddns/bin/activate
pip install --upgrade https://github.com/tysonhammen/py-unifi-route53-ddns/archive/refs/heads/main.zip
```

If you see `KeyError: 'ROUTE53_MY_DNS_NAME'`, the env file has `ROUTE53_MY_DNS_NAMES` but the device is still running an older build that only reads `ROUTE53_MY_DNS_NAME`. Either upgrade the package (steps above; use `pip install --no-cache-dir --upgrade ...` and then `systemctl restart py-unifi-route53-ddns.timer`) so the new code runs, or add the legacy variable to the env file: `Environment="ROUTE53_MY_DNS_NAME=your.host.example.com"` (one hostname). Then run `systemctl daemon-reload`.

Existing configs that use `ROUTE53_MY_DNS_NAME` (single host) continue to work. For multiple hostnames, set `ROUTE53_MY_DNS_NAMES` (comma-separated) in `/etc/systemd/system/py-unifi-route53-ddns.service.d/env.conf` and run `systemctl daemon-reload`.

### Monitoring
Use `systemctl status py-unifi-route53-ddns.service` or `journalctl -u py-unifi-route53-ddns.service` to see the status and logs of the service.

### WireGuard VPN configuration
The UniFi console provides a built-in WireGuard VPN. Navigate to Control Plane -> VPN -> VPN Server -> Create New, configure the server, and check "Use Alternate Address for Clients", then enter the FQDN that you configured as the dynamic hostname (or one of them) above. Any client added after this point (with a QR code or otherwise) will receive this configuration.

### IAM permissions
Use the visual editor to create a policy with the following permissions:
* Route53 `ListHostedZonesByName`
* Route53 `ListResourceRecordSets`
* Route53 `ChangeResourceRecordSets`

When asked for the resource, specify the zone ID of the Route53 hosted zone that you're using.

Or use the following policy JSON:
```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "route53:ChangeResourceRecordSets",
                "route53:ListResourceRecordSets"
            ],
            "Resource": "arn:aws:route53:::hostedzone/REPLACE_WITH_YOUR_HOSTED_ZONE_ID"
        },
        {
            "Effect": "Allow",
            "Action": "route53:ListHostedZonesByName",
            "Resource": "*"
        }
    ]
}
```

### Bugs

Please report bugs, issues, feature requests, etc. on [GitHub](https://github.com/tysonhammen/py-unifi-route53-ddns/issues).

### Links

* [UniFi Comparison Charts](https://evanmccann.net/blog/ubiquiti/unifi-comparison-charts) with technical information about different UniFi gateways

### License

Copyright 2024, Andrey Kislyuk and py-unifi-route53-ddns contributors. Licensed under the terms of the
[Apache License, Version 2.0](http://www.apache.org/licenses/LICENSE-2.0). Distribution of the LICENSE and NOTICE
files with source copies of this package and derivative works is **REQUIRED** as specified by the Apache License.
