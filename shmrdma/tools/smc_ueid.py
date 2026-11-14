#!/usr/bin/env python3
"""Add an SMC user EID, as `smcd ueid add` from smc-tools does.

SMC v2 connects two peers only when they share an Enterprise ID.  s390
hardware supplies a system EID; elsewhere both peers need the same user
EID, so every node runs this with the same value.

usage: smc_ueid.py [EID]    (default LTVM-SHMRDMA)
"""

import os
import socket
import struct
import sys

NETLINK_GENERIC = 16
GENL_ID_CTRL = 0x10
CTRL_CMD_GETFAMILY = 3
CTRL_ATTR_FAMILY_ID = 1
CTRL_ATTR_FAMILY_NAME = 2
NLM_F_REQUEST = 1
NLM_F_ACK = 4
NLMSG_ERROR = 2
SMC_NETLINK_ADD_UEID = 10
SMC_NLA_EID_TABLE_ENTRY = 1
SMC_MAX_EID_LEN = 32


def attr(t, data):
    a = struct.pack("HH", 4 + len(data), t) + data
    return a + b"\0" * (-len(a) % 4)


def request(s, family, cmd, payload, seq):
    body = struct.pack("BBH", cmd, 1, 0) + payload
    s.send(
        struct.pack(
            "IHHII",
            16 + len(body),
            family,
            NLM_F_REQUEST | NLM_F_ACK,
            seq,
            os.getpid(),
        )
        + body
    )
    reply = b""
    while True:
        buf = s.recv(65536)
        off = 0
        while off < len(buf):
            ln, typ, _, _, _ = struct.unpack_from("IHHII", buf, off)
            if typ == NLMSG_ERROR:
                err = struct.unpack_from("i", buf, off + 16)[0]
                if err:
                    raise OSError(-err, os.strerror(-err))
                return reply
            reply = buf[off + 20 : off + ln]
            off += (ln + 3) & ~3


def family_id(s, name):
    reply = request(
        s,
        GENL_ID_CTRL,
        CTRL_CMD_GETFAMILY,
        attr(CTRL_ATTR_FAMILY_NAME, name.encode() + b"\0"),
        1,
    )
    off = 0
    while off < len(reply):
        ln, t = struct.unpack_from("HH", reply, off)
        if t == CTRL_ATTR_FAMILY_ID:
            return struct.unpack_from("H", reply, off + 4)[0]
        off += (ln + 3) & ~3
    raise RuntimeError("no family id for " + name)


def main():
    eid = (sys.argv[1] if len(sys.argv) > 1 else "LTVM-SHMRDMA").upper()
    entry = eid.ljust(SMC_MAX_EID_LEN)[:SMC_MAX_EID_LEN].encode() + b"\0"
    s = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, NETLINK_GENERIC)
    fam = family_id(s, "SMC_GEN_NETLINK")
    try:
        request(
            s,
            fam,
            SMC_NETLINK_ADD_UEID,
            attr(SMC_NLA_EID_TABLE_ENTRY, entry),
            2,
        )
    except OSError as e:
        if e.errno != 17:  # EEXIST: already there
            raise
    print("ueid", eid)


if __name__ == "__main__":
    main()
