# IPv6 on ltvm clusters

Every extra (`--nic`) NIC holds an IPv4 address and an IPv6 address.
The management NIC (`eth0`) holds IPv4 only. A cluster's NIDs use one
family at a time, and `cluster deploy --ip-family` picks it.

Linux VMs with two to eight data NICs can also select a
[shared or isolated multi-rail profile](MULTIRAIL.md). The profile
configures source policy routing for both IP families and keeps `eth0`
outside LNet.

## Run a cluster on IPv6

IPv6 needs tcp on an extra NIC:

```bash
sudo ltvm cluster create co6 rocky9 mgs+mds:co6-mds:1 oss:co6-oss:2 \
    client:co6-cli --nic tcp
ltvm cluster deploy co6 --build ~/lustre-release --ip-family ipv6
```

`--ip-family ipv6` writes these lines into each node's `cfg/local.sh`:

```bash
NETTYPE=tcp
FORCE_LARGE_NID=true
MGSNID=fd17:2016:1000:f100:f172:f016:f100:f023@tcp
```

The cluster records the family, so a bare `cluster deploy` keeps it.
`--ip-family ipv4` writes `FORCE_LARGE_NID=false` and returns to IPv4.
`ltvm cluster status` shows the family.

The deploy reads the MGS NID from the MGS itself. It takes the first
net in the MGS's `/etc/modprobe.d/lnet.conf` and the first interface of
that net. It refuses `--ip-family ipv6` before the build in two cases:

- The net is not tcp. `test-framework.sh` stops with
  `FORCE_LARGE_NID only supported by tcp`.
- The interface has no global IPv6 address. `eth0` has none, so a
  cluster with no `--nic` cannot run IPv6.

## Why lnet.conf does not change

`lnet.conf` is the same for both families, for example
`options lnet networks="tcp0(eth1)"`. It names interfaces, not
addresses. LNet selects the family when the node configures it.
`lnetctl lnet configure --large` puts each interface's IPv6 address
first. `FORCE_LARGE_NID=true` makes the test framework use `--large`.

## How ltvm derives the addresses

Without an explicit multi-rail profile, the default prefix is
`fd17:2016:1000:f100::/64`. The interface ID
holds the four IPv4 octets. Each octet is `f` and then the octet in
three decimal digits:

```
172.16.100.203  ->  fd17:2016:1000:f100:f172:f016:f100:f203
```

Every hextet is `0x1000` or more, so the address always prints at full
width: 39 characters with no `::`. The resulting 43-character NID tests
the string sizes that a short address does not reach. Set another
prefix with `$LTVM_EXTRA_SUBNET6` or `VM_DIR/extra-subnet6`. ltvm
refuses a prefix that breaks the `0x1000` rule.

Explicit profiles use fixed prefixes instead: shared mode uses
`fd17:2016:2000:1000::/64`, and isolated rails 1 through 8 use
`fd17:2016:2000:1001::/64` through `fd17:2016:2000:1008::/64`.
They keep the same full-width interface ID encoding. Legacy prefix
overrides do not change these profiles. See the
[multi-rail fabric table](MULTIRAIL.md#fixed-fabrics-and-ownership).

`eth0` has no IPv6 address, on purpose. `test-framework.sh` makes NIDs
from the output of `hostname -I`. An IPv6 address on `eth0` could then
become a NID.

## Check that a node is on IPv6

```bash
ssh co6-mds 'modprobe lnet; lnetctl lnet configure --all --large; lctl list_nids'
ssh co6-cli 'modprobe lnet; lnetctl lnet configure --all --large; \
    lctl ping fd17:2016:1000:f100:f172:f016:f100:f023@tcp'
```

`lctl list_nids` on the MGS must print the `MGSNID` from `cfg/local.sh`,
character for character. An IPv4 NID means that LNet did not take the
IPv6 address. A compressed IPv6 NID means that the prefix breaks the
width rule.

## Upstream IPv6 limits

LNet IPv6 support does not by itself prove filesystem mount or LNet
self-test support in a particular Lustre build. Test these separately.

Lustre builds affected by LU-18041 reject long IPv6 NIDs during mount.
The limit is 40 for `UUID_MAX`, so an `obd_uuid` holds a NID of 39
characters at most. ltvm's full-width IPv6 NIDs exceed that limit. The
MDT mount fails with `-EINVAL` and this diagnostic:

```
LustreError: (ldlm_lib.c:369:client_obd_setup()) target UUID must be 40 characters or less
```

This error means that the build cannot mount this IPv6 topology.
Changing to IPv4 does not validate IPv6 filesystem I/O. An LNet suite
can still run when auster skips its own format and mount:

```bash
ssh co6-cli 'cd /usr/lib64/lustre/tests && ./auster -N -v sanity-lnet'
```

Some `lst` builds use the older 64-bit NID format and reject IPv6 NIDs
with `Invalid nid:`. The group command can then delete its empty group
and return zero. Check the diagnostic output and actual group members.
In that case, `lctl ping` and LNet discovery can
check IPv6 reachability, but they cannot prove IPv6 payload balancing.
The [multi-rail live tests](MULTIRAIL.md#reproduce-the-tests) keep
topology, filesystem payload, and self-test checks separate. They
recognize only these specific upstream errors as expected failures;
other setup, mount, and payload errors fail the test.
