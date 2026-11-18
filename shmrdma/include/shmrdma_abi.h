/* SPDX-License-Identifier: GPL-2.0 OR BSD-2-Clause */
/*
 * shmrdma: RDMA between VMs on one host through shared guest memory.
 *
 * Interface between the guest driver (shmrdma.ko) and the host backend
 * (shmrdmad), a vhost-user device.  Everything here is little endian
 * and laid out identically in the kernel and in userspace.
 *
 * Rings live in guest memory.  The guest allocates them, describes
 * them to the backend by guest physical address (GPA), and the
 * backend reads and writes them directly through its mapping of guest
 * RAM.  The virtqueues carry only control commands (ctrl), interrupts
 * (event) and doorbells (doorbell).
 */
#ifndef SHMRDMA_ABI_H
#define SHMRDMA_ABI_H

#ifdef __KERNEL__
#include <linux/types.h>
#include <linux/stddef.h>
#elif defined(__linux__)
#include <linux/types.h>
#include <stddef.h>
#else
/* the host daemon on macOS: the same fixed-width types */
#include <stddef.h>
#include <stdint.h>
typedef uint8_t __u8;
typedef uint16_t __u16;
typedef uint32_t __u32;
typedef uint64_t __u64;
typedef int32_t __s32;
#endif

/*
 * QEMU does not let users create its generic vhost-user-device, so the
 * device is a vhost-user-fs-pci with tag "shmrdma" and two request
 * queues (three vqs).  The guest driver claims only that tag.
 */
#define SHMRDMA_VIRTIO_ID	26	/* VIRTIO_ID_FS */
#define SHMRDMA_TAG		"shmrdma"
#define SHMRDMA_ABI_VERSION	4

enum {
	SHMRDMA_VQ_CTRL = 0,	/* guest -> host commands, host replies */
	SHMRDMA_VQ_EVENT = 1,	/* host -> guest: "look at the EQ" */
	SHMRDMA_VQ_DOORBELL = 2,	/* guest kicks only, never has buffers */
	SHMRDMA_NUM_VQS = 3,
};

/* Key 0 is the local DMA key: an SGE address is a GPA. */
#define SHMRDMA_LOCAL_DMA_LKEY	0
/* MR keys: index << 8 | variant byte (ib_update_fast_reg_key). */
#define SHMRDMA_KEY_INDEX(k)	((k) >> 8)

#define SHMRDMA_MAX_MR		65536
#define SHMRDMA_MAX_QP		4096	/* qpn < this */
#define SHMRDMA_MAX_CQ		8192	/* cqn < this; also EQ size */
#define SHMRDMA_MAX_GID		32
#define SHMRDMA_MAX_FRPL	4096	/* fast-reg page list length */
#define SHMRDMA_MAX_QP_WR	16384
#define SHMRDMA_MAX_CQE		(1 << 20)
#define SHMRDMA_MAX_SGE		16
#define SHMRDMA_GRH_BYTES	40
#define SHMRDMA_MAX_MSG		0x80000000u	/* port max_msg_sz */

/*
 * A ring: entries of entry_size bytes in pages listed at pages_gpa,
 * exactly the pages the entries span.  Ring pages are 4 KB whatever the
 * guest's page size: a 16 KB or 64 KB page kernel lists each 4 KB piece
 * of its pages.  At most SHMRDMA_MAX_CQE entries in a CQ and
 * SHMRDMA_MAX_QP_WR in a work queue.
 */
#define SHMRDMA_RING_PAGE_SHIFT	12
#define SHMRDMA_RING_PAGE_SIZE	(1u << SHMRDMA_RING_PAGE_SHIFT)

struct shmrdma_ring_desc {
	__u64 pages_gpa;	/* GPA of __u64 page GPA array */
	__u32 npages;
	__u32 entries;		/* power of 2 */
	__u32 entry_size;	/* power of 2, <= 4096 */
	__u32 pad;
};

/* Device-wide shared page, GPA given in INIT. */
struct shmrdma_dev_shared {
	/* written by the backend */
	__u32 eq_prod;
	__u32 eq_lost;		/* 1: an EQE did not fit; the guest clears it */
	__u32 pad0[14];
	/* written by the guest */
	__u32 eq_cons;
	__u32 eq_armed;		/* 1: guest wants an interrupt for new EQEs */
	__u32 pad1[14];
};

/* EQ entry: a CQ that has a completion event. */
struct shmrdma_eqe {
	__u32 cqn;
	__u32 type;		/* SHMRDMA_EQE_* */
};

enum {
	SHMRDMA_EQE_COMP = 0,
	SHMRDMA_EQE_QP_FATAL = 1,	/* cqn holds the qpn */
	SHMRDMA_EQE_CQ_ERR = 2,		/* overflowed: its QPs go to error */
};

/* Per-QP doorbell record, 128 bytes; records are at least 8-aligned. */
struct shmrdma_qp_rec {
	/* written by the guest */
	__u32 sq_prod;
	__u32 rq_prod;
	__u32 pad0[14];
	/* written by the backend; by the guest once it reset the device */
	__u32 sq_cons;
	__u32 rq_cons;
	__u32 need_kick;	/* backend idle: guest must ring the doorbell */
	__u32 fatal;		/* 1: QP_FATAL raised, read after eq_lost */
	/*
	 * A backend frees a WR's slot (*_cons), then
	 * makes its CQE visible, then moves *_done up to *_cons.  WR *_done
	 * of a backend that died between the two is described here, since
	 * its slot may hold a new WR: *_limbo_op is SHMRDMA_LIMBO_NONE when
	 * it has no CQE, else its CQE is at CQ index *_limbo_pos.
	 */
	__u32 sq_done;
	__u32 rq_done;
	__u32 sq_limbo_pos;
	__u32 rq_limbo_pos;
	__u64 sq_limbo_wr_id;
	__u64 rq_limbo_wr_id;
	__u32 sq_limbo_op;	/* SHMRDMA_WC_* */
	__u32 rq_limbo_op;
	__u32 pad1[2];
};

#define SHMRDMA_LIMBO_NONE	0xffffffffu

/*
 * After the backend died, with the CQ's producer final: whether the
 * WR *_done still needs a flush completion.
 */
static inline int shmrdma_limbo_owed(__u32 done, __u32 cons, __u32 op,
				     __u32 pos, __u32 cq_prod)
{
	return cons == done + 1 && op != SHMRDMA_LIMBO_NONE &&
	       (__s32)(cq_prod - pos) <= 0;
}

/* Per-CQ record, 128 bytes. */
struct shmrdma_cq_rec {
	/* written by the backend */
	__u32 prod;
	__u32 err;		/* 1: CQ_ERR raised, read after eq_lost */
	__u32 pad0[14];
	/* written by the guest; arm is cleared by the backend */
	__u32 cons;
	__u32 arm;		/* SHMRDMA_CQ_ARM_* */
	__u32 pad1[14];
};

enum {
	SHMRDMA_CQ_ARM_NONE = 0,
	SHMRDMA_CQ_ARM_NEXT = 1,
	SHMRDMA_CQ_ARM_SOLICITED = 2,
};

struct shmrdma_sge {
	__u64 addr;
	__u32 length;
	__u32 lkey;
};

enum {
	SHMRDMA_OP_SEND = 0,
	SHMRDMA_OP_SEND_IMM = 1,
	SHMRDMA_OP_SEND_INV = 2,
	SHMRDMA_OP_RDMA_WRITE = 3,
	SHMRDMA_OP_RDMA_WRITE_IMM = 4,
	SHMRDMA_OP_RDMA_READ = 5,
	SHMRDMA_OP_REG_MR = 6,
	SHMRDMA_OP_LOCAL_INV = 7,
};

enum {
	SHMRDMA_WQE_SIGNALED = 1 << 0,
	SHMRDMA_WQE_SOLICITED = 1 << 1,
	SHMRDMA_WQE_INLINE = 1 << 2,	/* data in sge area, length in inline_len */
	SHMRDMA_WQE_FENCE = 1 << 3,
};

/*
 * MR access, our own bits.  An MR is in the protection domain of the QP
 * its REG_MR is posted on, and its keys work only on QPs of that PD; the
 * local DMA key works on every QP of the device.
 */
enum {
	SHMRDMA_ACC_LOCAL_WRITE = 1 << 0,
	SHMRDMA_ACC_REMOTE_WRITE = 1 << 1,
	SHMRDMA_ACC_REMOTE_READ = 1 << 2,
};

#define SHMRDMA_WQE_HDR	64

struct shmrdma_sq_wqe {
	__u64 wr_id;
	__u8 opcode;
	__u8 flags;
	__u8 num_sge;
	__u8 pad;
	__u32 ex;		/* imm data (BE as given) or rkey to invalidate */
	union {
		struct {
			__u64 remote_addr;
			__u32 rkey;
			__u32 inline_len;
		} rdma;
		struct {
			__u32 dqpn;
			__u32 qkey;
			__u8 dgid[16];
			__u8 sgid[16];
		} ud;
		struct {
			__u64 iova;
			__u64 length;
			__u64 pages_gpa;
			__u32 key;
			__u32 npages;
			__u32 access;
			__u8 page_shift;
			__u8 pad[3];
		} reg;
		struct {
			__u64 pad;
			__u32 pad2;
			__u32 inline_len;
		} send;
	};
	__u64 pad2;
	struct shmrdma_sge sge[];
};

struct shmrdma_rq_wqe {
	__u64 wr_id;
	__u32 num_sge;
	__u32 pad;
	struct shmrdma_sge sge[];
};

enum {
	SHMRDMA_CQE_GRH = 1 << 0,
	SHMRDMA_CQE_IMM = 1 << 1,
	SHMRDMA_CQE_INV = 1 << 2,
	SHMRDMA_CQE_IPV4 = 1 << 3,
};

/* CQE opcodes: our own, mapped to ib_wc_opcode by the guest. */
enum {
	SHMRDMA_WC_SEND = 0,
	SHMRDMA_WC_RDMA_WRITE = 1,
	SHMRDMA_WC_RDMA_READ = 2,
	SHMRDMA_WC_REG_MR = 3,
	SHMRDMA_WC_LOCAL_INV = 4,
	SHMRDMA_WC_RECV = 5,
	SHMRDMA_WC_RECV_RDMA_IMM = 6,
};

/* CQE statuses: ib_wc_status values, which are stable ABI. */
struct shmrdma_cqe {
	__u64 wr_id;
	__u32 qpn;
	__u8 opcode;
	__u8 status;
	__u8 flags;
	__u8 pad;
	__u32 byte_len;
	__u32 ex;		/* imm data or invalidated rkey */
	__u32 src_qp;
	__u32 vendor_err;
};

/* ---- control commands ---- */

enum {
	SHMRDMA_CMD_INIT = 1,
	SHMRDMA_CMD_ADD_GID,
	SHMRDMA_CMD_DEL_GID,
	SHMRDMA_CMD_CREATE_CQ,
	SHMRDMA_CMD_DESTROY_CQ,
	SHMRDMA_CMD_CREATE_QP,
	SHMRDMA_CMD_MODIFY_QP,
	SHMRDMA_CMD_DESTROY_QP,
	SHMRDMA_CMD_DEREG_MR,
	SHMRDMA_CMD_RESET,
};

enum {
	SHMRDMA_QPT_RC = 0,
	SHMRDMA_QPT_UD = 1,
	SHMRDMA_QPT_GSI = 2,
};

/* QP states: ib_qp_state values */

struct shmrdma_cmd {
	__u32 cmd;
	__u32 pad;
	union {
		struct {
			__u32 abi;
			__u32 eq_entries;
			__u64 shared_gpa;
			__u64 eq_gpa;		/* physically contiguous */
		} init;
		struct {
			__u32 index;
			__u32 pad;
			__u8 gid[16];
		} gid;
		struct {
			__u32 cqn;
			__u32 pad;
			__u64 rec_gpa;
			struct shmrdma_ring_desc ring;
		} create_cq;
		struct {
			__u32 cqn;
		} destroy_cq;
		struct {
			__u32 qpn;
			__u32 type;
			__u32 send_cqn;
			__u32 recv_cqn;
			__u64 rec_gpa;
			struct shmrdma_ring_desc sq;
			struct shmrdma_ring_desc rq;
			__u32 pdn;	/* protection domain */
			__u32 pad;
		} create_qp;
		struct {
			__u32 qpn;
			__u32 mask;	/* SHMRDMA_MQ_* */
			__u32 state;
			__u32 dest_qpn;
			__u32 qkey;
			__u32 rnr_retry;
			__u32 retry_cnt;
			__u32 access;	/* SHMRDMA_ACC_REMOTE_*: what peers may do */
			__u8 dgid[16];
			__u32 timeout;	/* ack timeout: 4.096 us << timeout */
		} modify_qp;
		struct {
			__u32 qpn;
		} destroy_qp;
		struct {
			__u32 key;
		} dereg_mr;
		__u8 raw[120];
	};
};

enum {
	SHMRDMA_MQ_STATE = 1 << 0,
	SHMRDMA_MQ_DEST = 1 << 1,	/* dgid + dest_qpn */
	SHMRDMA_MQ_QKEY = 1 << 2,
	SHMRDMA_MQ_RETRY = 1 << 3,
	SHMRDMA_MQ_ACCESS = 1 << 4,
	/*
	 * retry_cnt and timeout, which a backend that knows no faults
	 * ignores; added without an ABI bump, since a backend before it
	 * ignores the bit and a driver before it never sets it.
	 */
	SHMRDMA_MQ_TIMEOUT = 1 << 5,
};

struct shmrdma_resp {
	__s32 status;		/* 0 or -errno */
	__u32 abi;		/* INIT: the backend's SHMRDMA_ABI_VERSION */
	__u64 val;		/* INIT: the backend's device number */
};

#define SHMRDMA_ABI_SIZE(t, n) \
	_Static_assert(sizeof(struct t) == (n), #t " size is ABI")

SHMRDMA_ABI_SIZE(shmrdma_ring_desc, 24);
SHMRDMA_ABI_SIZE(shmrdma_dev_shared, 128);
SHMRDMA_ABI_SIZE(shmrdma_eqe, 8);
SHMRDMA_ABI_SIZE(shmrdma_qp_rec, 128);
_Static_assert(offsetof(struct shmrdma_qp_rec, sq_limbo_wr_id) % 8 == 0,
	       "limbo wr_ids are 8-aligned");
SHMRDMA_ABI_SIZE(shmrdma_cq_rec, 128);
SHMRDMA_ABI_SIZE(shmrdma_sge, 16);
SHMRDMA_ABI_SIZE(shmrdma_sq_wqe, SHMRDMA_WQE_HDR);
SHMRDMA_ABI_SIZE(shmrdma_rq_wqe, 16);
SHMRDMA_ABI_SIZE(shmrdma_cqe, 32);
SHMRDMA_ABI_SIZE(shmrdma_cmd, 128);
SHMRDMA_ABI_SIZE(shmrdma_resp, 16);
_Static_assert(offsetof(struct shmrdma_sq_wqe, sge) == SHMRDMA_WQE_HDR,
	       "SGEs start after the 64-byte WQE header");

#endif
