// SPDX-License-Identifier: GPL-2.0
/*
 * shmrdmad: host backend for the shmrdma virtual RDMA device.
 *
 * One process serves every VM on the host.  Each QEMU connects to the
 * listening vhost-user socket and hands over its guest RAM (memfd,
 * share=on).  Work requests that the guest posts in its own memory are
 * executed here as memcpy between the two guests' RAM: one copy per
 * byte, no intermediate buffer.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <inttypes.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>


#include "../include/shmrdma_abi.h"
#include "plat.h"

/* ---------------------------------------------------------------- */
/* utilities                                                        */

static int g_verbose;

#define LOG(fmt, ...) do { \
	struct timespec _ts; clock_gettime(CLOCK_REALTIME, &_ts); \
	fprintf(stderr, "%ld.%03ld shmrdmad: " fmt "\n", (long)_ts.tv_sec, \
		_ts.tv_nsec / 1000000, ##__VA_ARGS__); } while (0)
#define DBG(fmt, ...) do { if (g_verbose) LOG(fmt, ##__VA_ARGS__); } while (0)

#define ACQ(p)		__atomic_load_n((p), __ATOMIC_ACQUIRE)
#define RLX(p)		__atomic_load_n((p), __ATOMIC_RELAXED)
#ifndef REL
#define REL(p, v)	__atomic_store_n((p), (v), __ATOMIC_RELEASE)
#endif
#define FENCE()		__atomic_thread_fence(__ATOMIC_SEQ_CST)

/* ib_wc_status values */
enum {
	WC_SUCCESS = 0, WC_LOC_LEN_ERR = 1, WC_LOC_QP_OP_ERR = 2,
	WC_LOC_PROT_ERR = 4, WC_WR_FLUSH_ERR = 5, WC_BAD_RESP_ERR = 7,
	WC_LOC_ACCESS_ERR = 8, WC_REM_INV_REQ_ERR = 9, WC_REM_ACCESS_ERR = 10,
	WC_REM_OP_ERR = 11, WC_RETRY_EXC_ERR = 12, WC_RNR_RETRY_EXC_ERR = 13,
	WC_GENERAL_ERR = 21,
};

/* ib_qp_state values */
enum { QPS_RESET, QPS_INIT, QPS_RTR, QPS_RTS, QPS_SQD, QPS_SQE, QPS_ERR };

/* ---------------------------------------------------------------- */
/* vhost-user protocol                                              */

enum {
	VU_GET_FEATURES = 1, VU_SET_FEATURES = 2, VU_SET_OWNER = 3,
	VU_RESET_OWNER = 4, VU_SET_MEM_TABLE = 5, VU_SET_LOG_BASE = 6,
	VU_SET_LOG_FD = 7, VU_SET_VRING_NUM = 8, VU_SET_VRING_ADDR = 9,
	VU_SET_VRING_BASE = 10, VU_GET_VRING_BASE = 11, VU_SET_VRING_KICK = 12,
	VU_SET_VRING_CALL = 13, VU_SET_VRING_ERR = 14,
	VU_GET_PROTOCOL_FEATURES = 15, VU_SET_PROTOCOL_FEATURES = 16,
	VU_GET_QUEUE_NUM = 17, VU_SET_VRING_ENABLE = 18,
	VU_RESET_DEVICE = 34,
};

#define VU_VERSION	0x1
#define VU_FLAG_REPLY	(1u << 2)
#define VU_FLAG_NEED_REPLY (1u << 3)
#define VU_VRING_NOFD	(1ull << 8)
#define VU_MAX_REGIONS	8

#define F_VERSION_1		(1ull << 32)
#define F_PROTOCOL_FEATURES	(1ull << 30)
#define F_RING_INDIRECT_DESC	(1ull << 28)
#define F_RING_EVENT_IDX	(1ull << 29)

#define NET_MAX_QP		8	/* virtio-net queue pairs per port */
#define MAX_VQS			(2 * NET_MAX_QP)
#define PF_MQ			(1ull << 0)
#define PF_REPLY_ACK		(1ull << 3)
#define PF_RESET_DEVICE		(1ull << 13)

struct vu_hdr {
	uint32_t request;
	uint32_t flags;
	uint32_t size;
} __attribute__((packed));

struct vu_region {
	uint64_t gpa;
	uint64_t size;
	uint64_t uaddr;
	uint64_t mmap_offset;
};

struct vu_memtab {
	uint32_t nregions;
	uint32_t pad;
	struct vu_region r[VU_MAX_REGIONS];
};

struct vu_state {
	uint32_t index;
	uint32_t num;
};

struct vu_addr {
	uint32_t index;
	uint32_t flags;
	uint64_t desc;
	uint64_t used;
	uint64_t avail;
	uint64_t log;
};

union vu_payload {
	uint64_t u64;
	struct vu_state state;
	struct vu_addr addr;
	struct vu_memtab mem;
	uint8_t raw[4096];
};

/* split virtqueue layout */
struct vring_desc {
	uint64_t addr;
	uint32_t len;
	uint16_t flags;
	uint16_t next;
};
#define VRING_DESC_F_NEXT	1
#define VRING_DESC_F_WRITE	2
#define VRING_DESC_F_INDIRECT	4
#define VRING_AVAIL_F_NO_INTERRUPT 1

struct vring_avail {
	uint16_t flags;
	uint16_t idx;
	uint16_t ring[];
};

/*
 * Guest memory changes under us: what is checked must be what is used,
 * so a descriptor or ring entry is read once, field by field, into a
 * copy the compiler may not reload from the guest.
 */
static inline struct vring_desc desc_load(const struct vring_desc *p)
{
	struct vring_desc d = {
		.addr = RLX(&p->addr),
		.len = RLX(&p->len),
		.flags = RLX(&p->flags),
		.next = RLX(&p->next),
	};

	return d;
}

struct vring_used_elem {
	uint32_t id;
	uint32_t len;
};

struct vring_used {
	uint16_t flags;
	uint16_t idx;
	struct vring_used_elem ring[];
};

/* ---------------------------------------------------------------- */
/* objects                                                          */

enum { EP_LISTEN = 1, EP_SOCK, EP_KICK, EP_LISTEN_NET };

struct vdev;

struct ep_tag {
	int kind;
	struct vdev *d;
	int vq;
};

struct mapping {
	uint64_t gpa, size, uaddr, offset;
	uint64_t st_dev, st_ino;	/* the file it maps */
	uint8_t *hva;		/* hva of gpa */
	void *map_base;
	size_t map_len;
};

struct vq {
	uint32_t num;
	struct vring_desc *desc;
	struct vring_avail *avail;
	struct vring_used *used;
	uint64_t desc_ua, avail_ua, used_ua;
	uint16_t last_avail;
	uint16_t used_idx;
	int kickfd;
	int callfd;
	bool enabled;
	bool started;
	bool event_idx;		/* VIRTIO_RING_F_EVENT_IDX negotiated */
	bool signalled_valid;
	uint16_t signalled_used;
	bool broken;		/* guest index past the ring; stopped until reset */
	/* what GET_VRING_BASE left, while its device is stopped */
	bool mark;
	uint32_t mark_num;
	uint16_t mark_base;
	uint64_t mark_desc, mark_avail, mark_used;
	struct ep_tag kick_tag;
};

struct ring {
	uint32_t entries;
	uint32_t esz;
	uint32_t npages;
	uint8_t **pages;
};

static inline void *ring_entry(struct ring *r, uint32_t idx)
{
	uint64_t off = (uint64_t)(idx & (r->entries - 1)) * r->esz;

	return r->pages[off >> 12] + (off & 4095);
}

struct cq {
	struct vdev *dev;
	uint32_t cqn;
	struct shmrdma_cq_rec *rec;
	struct ring ring;
	uint32_t prod;
	plat_spinlock_t lock;
	bool overflowed;
};

struct mr {
	uint32_t key;
	uint32_t access;
	uint64_t iova;
	uint64_t length;
	uint32_t page_shift;
	uint32_t npages;
	uint32_t fpo;		/* offset of iova in the first page */
	uint32_t pdn;		/* the PD of the QP that registered it */
	bool valid;
	uint32_t cap;		/* entries pages[] has room for */
	uint64_t *pages;	/* grows, never shrinks or moves once used */
};

struct worker;

struct qp {
	struct vdev *dev;
	uint32_t qpn;
	uint32_t type;
	uint32_t state;
	struct cq *scq, *rcq;
	struct shmrdma_qp_rec *rec;
	struct ring sq, rq;
	uint32_t sq_cons, rq_cons;
	uint8_t dgid[16];
	uint32_t dqpn;
	uint32_t qkey;
	uint32_t rnr_retry;
	uint32_t pdn;
	uint32_t access;	/* SHMRDMA_ACC_REMOTE_* its peer may use */
	struct qp *peer;
	uint64_t peer_gen;
	plat_spinlock_t rq_lock;
	struct worker *w;
	uint64_t stall_since;
	bool fatal_sent;
	/* set by other workers; the owner moves the QP to error */
	int err_req;
	/* stats */
	uint64_t tx_bytes, tx_wr;
};

struct netport;
struct netq;
static struct vdev *netq_dev(struct netq *nq);

enum { KIND_RDMA, KIND_NET };

struct vdev {
	int id;
	int sock;
	int kind;
	struct netport *port;
	struct ep_tag sock_tag;
	bool dead;
	/* QEMU stopped it (GET_VRING_BASE): nothing touches its memory
	 * until its rings come back as they were */
	bool stopped;
	uint64_t features;
	uint64_t pfeatures;
	int nmap;
	struct mapping map[VU_MAX_REGIONS];
	/* old mappings kept until teardown: cached pointers may use them */
	int nold;
	struct mapping old[64];
	struct vq vq[MAX_VQS];
	plat_spinlock_t evq_lock;	/* event vq + EQ */

	bool initialized;
	struct shmrdma_dev_shared *shared;
	struct shmrdma_eqe *eq;
	uint32_t eq_entries;
	uint32_t eq_prod;
	bool irq_pending;		/* armed but no event buffer */

	uint8_t gids[SHMRDMA_MAX_GID][16];
	bool gid_valid[SHMRDMA_MAX_GID];

	struct qp *qps[SHMRDMA_MAX_QP];
	struct cq *cqs[SHMRDMA_MAX_CQ];
	struct mr **mrs;		/* SHMRDMA_MAX_MR pointers */
	uint32_t ring_pages;		/* in every ring of the device */
	uint32_t mr_pages;		/* page list entries of its MRs, mr_lock */
	/* Workers register, invalidate and translate MRs concurrently;
	 * freeing one needs wlock. */
	plat_spinlock_t mr_lock;

	/* messages the guest can cause: a burst, then one a second */
	plat_spinlock_t log_lock;
	uint64_t log_t;
	uint32_t log_tokens, log_dropped;

	struct vdev *next;
};

struct worker {
	int idx;
	pthread_t thr;
	pthread_mutex_t mtx;
	int evfd;		/* wake notifier, read end */
	int evfd_w;		/* and its write end (the same fd on Linux) */
	int epfd;		/* poller: evfd and the doorbells of its devices */
	int sleeping;
	struct qp **qps;
	int nqps, cap;
	struct netq **nqs;
	int nnq, nqcap;
};

/* ---------------------------------------------------------------- */
/* globals                                                          */

static struct vdev *g_devs;
static int g_next_dev_id;
static uint64_t g_gen = 1;		/* bumped when any QP/dev goes away */
static int g_nworkers = 2;
static struct worker *g_workers;
static int g_epfd;
static uint64_t g_spin_ns = 200000;	/* spin before sleeping */
static uint64_t g_rnr_timeout_ns = 2000000000ull;
static const char *g_sock_path = "/tmp/shmrdma.sock";
static const char *g_net_sock_path = "/tmp/shmnet.sock";
static int g_lsock_net = -1;
static int g_ncopiers = 0;		/* helper threads for large copies */
static uint64_t g_split_min = 256 << 10;	/* split copies at least this big */
static uint64_t g_part_min = 128 << 10;	/* into parts at least this big */
static uint64_t g_nt_min = 64 << 10;	/* non-temporal stores from here */
static uint64_t g_net_nt_min = 16 << 10;	/* the same for shmnet frames */
static uint64_t g_net_part_min = 30 << 10;	/* split frames into these */
static int g_lsock;

static int g_wwait;	/* wlock() is waiting for the workers' mutexes */

/* writer lock: hold every worker's mutex */
static void wlock(void)
{
	__atomic_add_fetch(&g_wwait, 1, __ATOMIC_SEQ_CST);
	for (int i = 0; i < g_nworkers; i++)
		pthread_mutex_lock(&g_workers[i].mtx);
	__atomic_sub_fetch(&g_wwait, 1, __ATOMIC_SEQ_CST);
}

/* a worker that has just unlocked: mutexes are not fair, so let a
 * waiting wlock() have this one before taking it again */
static void worker_yield(void)
{
	while (__atomic_load_n(&g_wwait, __ATOMIC_SEQ_CST))
		cpu_relax();
}

/* a worker holding its mutex, between units of work */
static void worker_let_in(struct worker *w)
{
	if (!__atomic_load_n(&g_wwait, __ATOMIC_SEQ_CST))
		return;
	pthread_mutex_unlock(&w->mtx);
	worker_yield();
	pthread_mutex_lock(&w->mtx);
}

static void wunlock(void)
{
	for (int i = g_nworkers - 1; i >= 0; i--)
		pthread_mutex_unlock(&g_workers[i].mtx);
}

/* wake sleeping workers; only those with a QP on d, unless d is NULL */
static void wake_workers_for(struct vdev *d)
{
	for (int i = 0; i < g_nworkers; i++) {
		struct worker *w = &g_workers[i];
		bool mine = !d;

		if (!__atomic_load_n(&w->sleeping, __ATOMIC_SEQ_CST))
			continue;
		if (!mine) {
			pthread_mutex_lock(&w->mtx);
			for (int j = 0; j < w->nqps && !mine; j++)
				mine = w->qps[j]->dev == d;
			for (int j = 0; j < w->nnq && !mine; j++)
				mine = netq_dev(w->nqs[j]) == d;
			pthread_mutex_unlock(&w->mtx);
		}
		if (mine)
			plat_notifier_signal(w->evfd_w);
	}
}

static void wake_workers(void)
{
	wake_workers_for(NULL);
}

/*
 * The doorbell of a device a worker serves goes straight into the
 * worker's poller, edge-triggered: every guest kick wakes it with no
 * hop through the main thread, and nobody reads the eventfd.  Where
 * kick fds must be drained (macOS pipes), a doorbell has one reader
 * instead, the main thread, whose on_kick() wakes the workers: several
 * workers draining one fd would lose each other's wakeups.
 */
static void worker_watch(struct worker *w, int fd)
{
	if (fd < 0 || PLAT_NOTIFIER_DRAIN)
		return;
	if (plat_poller_add(w->epfd, fd, (void *)(intptr_t)fd, true))
		LOG("worker%d: watch fd %d: %s", w->idx, fd, strerror(errno));
}

static void worker_unwatch(struct worker *w, int fd)
{
	if (fd < 0 || PLAT_NOTIFIER_DRAIN)
		return;
	plat_poller_del(w->epfd, fd);
}

/*
 * Before closing a kick fd: a watch lives as long as the file, which
 * QEMU still holds, not as long as our fd.
 */
static void worker_unwatch_all(int fd)
{
	for (int i = 0; i < g_nworkers; i++)
		worker_unwatch(&g_workers[i], fd);
}

#define GLOG_BURST	20

static bool glog_ok(struct vdev *d)
{
	uint64_t t = now_ns();
	uint32_t dropped = 0;
	bool ok = false;

	plat_spin_lock(&d->log_lock);
	if (t - d->log_t >= 1000000000ull) {
		uint64_t add = (t - d->log_t) / 1000000000ull;

		d->log_tokens = add >= GLOG_BURST - d->log_tokens ? GLOG_BURST :
				d->log_tokens + add;
		d->log_t = t;
	}
	if (d->log_tokens) {
		d->log_tokens--;
		dropped = d->log_dropped;
		d->log_dropped = 0;
		ok = true;
	} else {
		d->log_dropped++;
	}
	plat_spin_unlock(&d->log_lock);
	if (dropped)
		LOG("dev%d: %u messages suppressed", d->id, dropped);
	return ok;
}

#define GLOG(d, fmt, ...) do { \
	if (glog_ok(d)) \
		LOG(fmt, ##__VA_ARGS__); } while (0)

/* ---------------------------------------------------------------- */
/* guest memory                                                     */

static void unmap_all(struct mapping *m, int n)
{
	for (int i = 0; i < n; i++)
		if (m[i].map_base)
			munmap(m[i].map_base, m[i].map_len);
}

/* hva of gpa, and how many bytes are contiguous from there */
static inline uint8_t *gpa_hva(struct vdev *d, uint64_t gpa, uint64_t *avail)
{
	for (int i = 0; i < d->nmap; i++) {
		struct mapping *m = &d->map[i];

		if (gpa >= m->gpa && gpa - m->gpa < m->size) {
			if (avail)
				*avail = m->size - (gpa - m->gpa);
			return m->hva + (gpa - m->gpa);
		}
	}
	return NULL;
}

/* hva of [gpa, gpa+len), or NULL unless entirely in one region */
static void *gpa_range(struct vdev *d, uint64_t gpa, uint64_t len)
{
	uint64_t avail;
	uint8_t *p = gpa_hva(d, gpa, &avail);

	if (!p || avail < len)
		return NULL;
	return p;
}

static void *uaddr_hva(struct vdev *d, uint64_t ua, uint64_t len)
{
	for (int i = 0; i < d->nmap; i++) {
		struct mapping *m = &d->map[i];

		if (ua >= m->uaddr && ua - m->uaddr < m->size &&
		    m->size - (ua - m->uaddr) >= len)
			return m->hva + (ua - m->uaddr);
	}
	return NULL;
}

/* ---------------------------------------------------------------- */
/* virtqueues                                                       */

/*
 * The call fd to signal for the used entries just published, or -1.
 * Call it under whatever serialises publishing on q: the decision and
 * signalled_used must move with the used index, or two publishers can
 * each decide the other signals.  Signalling the fd may come after.
 */
static int vq_need_signal(struct vq *q)
{
	int fd = RLX(&q->callfd);

	if (fd < 0)
		return -1;
	FENCE();
	if (q->event_idx) {
		uint16_t ev = ACQ(&q->avail->ring[q->num]);
		uint16_t old = q->signalled_used, new = q->used_idx;
		bool valid = q->signalled_valid;

		q->signalled_used = new;
		q->signalled_valid = true;
		/* vring_need_event() */
		if (valid && (uint16_t)(new - ev - 1) >= (uint16_t)(new - old))
			return -1;
	} else if (ACQ(&q->avail->flags) & VRING_AVAIL_F_NO_INTERRUPT) {
		return -1;
	}
	return fd;
}

static void vq_signal(struct vq *q)
{
	int fd = vq_need_signal(q);

	if (fd >= 0)
		plat_notifier_signal(fd);
}

static void vq_push_used(struct vq *q, uint32_t head, uint32_t len)
{
	struct vring_used_elem *e = &q->used->ring[q->used_idx % q->num];

	e->id = head;
	e->len = len;
	q->used_idx++;
	REL(&q->used->idx, q->used_idx);
}

static bool vq_ready(struct vq *q)
{
	return q->started && q->enabled && q->desc && q->avail && q->used;
}

/* Deliver an interrupt through the event vq.  Caller holds evq_lock. */
static void dev_send_irq(struct vdev *d)
{
	struct vq *q = &d->vq[SHMRDMA_VQ_EVENT];
	uint16_t ai;

	if (d->stopped || !vq_ready(q) || q->broken) {
		d->irq_pending = true;
		return;
	}
	ai = ACQ(&q->avail->idx);
	if ((uint16_t)(ai - q->last_avail) > q->num) {
		GLOG(d, "dev%d: event vq avail %u is past %u", d->id, ai,
		    q->last_avail);
		q->broken = true;
	}
	if (q->broken || q->last_avail == ai) {
		d->irq_pending = true;
		return;
	}
	uint16_t head = RLX(&q->avail->ring[q->last_avail % q->num]);

	q->last_avail++;
	d->irq_pending = false;
	vq_push_used(q, head, 0);
	vq_signal(q);
}

/* an EQE the guest has not read is never overwritten: it is told instead */
static void dev_raise_eq(struct vdev *d, uint32_t cqn, uint32_t type)
{
	if (!d->eq)
		return;
	plat_spin_lock(&d->evq_lock);
	if (d->eq_prod - ACQ(&d->shared->eq_cons) >= d->eq_entries) {
		REL(&d->shared->eq_lost, 1);
	} else {
		struct shmrdma_eqe *e =
			&d->eq[d->eq_prod & (d->eq_entries - 1)];

		e->cqn = cqn;
		e->type = type;
		d->eq_prod++;
		REL(&d->shared->eq_prod, d->eq_prod);
	}
	FENCE();
	if (RLX(&d->shared->eq_armed)) {
		REL(&d->shared->eq_armed, 0);
		dev_send_irq(d);
	}
	plat_spin_unlock(&d->evq_lock);
}

/* ---------------------------------------------------------------- */
/* completion queues                                                */

static void qp_err_req(struct qp *qp);

/* a CQE was lost: fail every QP on the CQ, each time; tell the guest once */
static void cq_overflow(struct cq *cq, bool first)
{
	struct vdev *d = cq->dev;

	REL(&cq->rec->err, 1);
	if (first)
		dev_raise_eq(d, cq->cqn, SHMRDMA_EQE_CQ_ERR);
	for (int i = 0; i < SHMRDMA_MAX_QP; i++) {
		struct qp *qp = d->qps[i];

		if (qp && (qp->scq == cq || qp->rcq == cq))
			qp_err_req(qp);
	}
}

/* a WR's slot in the guest's QP record, freed before its CQE is seen */
struct wr_slot {
	uint32_t *cons;
	uint32_t next;
	uint32_t *pos;
	uint32_t *op;
};

static void cq_post(struct cq *cq, const struct shmrdma_cqe *cqe,
		    bool solicited, const struct wr_slot *s)
{
	bool event = false;

	plat_spin_lock(&cq->lock);
	uint32_t cons = ACQ(&cq->rec->cons);

	if (cq->prod - cons >= cq->ring.entries) {
		bool first = !cq->overflowed;

		if (first)
			GLOG(cq->dev, "dev%d cq%u overflow (prod %u cons %u)",
			    cq->dev->id, cq->cqn, cq->prod, cons);
		cq->overflowed = true;
		if (s) {
			REL(s->op, SHMRDMA_LIMBO_NONE);
			REL(s->cons, s->next);
		}
		plat_spin_unlock(&cq->lock);
		cq_overflow(cq, first);
		return;
	}
	memcpy(ring_entry(&cq->ring, cq->prod), cqe, sizeof(*cqe));
	if (s) {
		REL(s->pos, cq->prod);
		REL(s->cons, s->next);
	}
	cq->prod++;
	REL(&cq->rec->prod, cq->prod);
	FENCE();
	uint32_t arm = RLX(&cq->rec->arm);

	if (arm == SHMRDMA_CQ_ARM_NEXT ||
	    (arm == SHMRDMA_CQ_ARM_SOLICITED &&
	     (solicited || cqe->status != WC_SUCCESS))) {
		if (__atomic_compare_exchange_n(&cq->rec->arm, &arm, 0, false,
						__ATOMIC_SEQ_CST,
						__ATOMIC_SEQ_CST))
			event = true;
	}
	plat_spin_unlock(&cq->lock);
	if (event)
		dev_raise_eq(cq->dev, cq->cqn, SHMRDMA_EQE_COMP);
}

/*
 * Publish qp's consumer index next for one WR, completing it with c on
 * cq when c is given.  The guest may refill the slot as soon as its
 * CQE is visible, and a guest whose backend died flushes from what the
 * record says, so the order of the stores here is the protocol.
 */
static void wr_retire(struct qp *qp, bool sq, uint32_t next, struct cq *cq,
		      const struct shmrdma_cqe *c, bool solicited)
{
	struct shmrdma_qp_rec *r = qp->rec;
	struct wr_slot s = {
		.cons = sq ? &r->sq_cons : &r->rq_cons,
		.next = next,
		.pos = sq ? &r->sq_limbo_pos : &r->rq_limbo_pos,
		.op = sq ? &r->sq_limbo_op : &r->rq_limbo_op,
	};

	if (c) {
		REL(sq ? &r->sq_limbo_wr_id : &r->rq_limbo_wr_id, c->wr_id);
		REL(s.op, c->opcode);
		cq_post(cq, c, solicited, &s);
	} else {
		REL(s.op, SHMRDMA_LIMBO_NONE);
		REL(s.cons, next);
	}
	REL(sq ? &r->sq_done : &r->rq_done, next);
}

/* consumer indexes set outright, while no worker can reach the QP */
static void qp_set_cons(struct qp *qp, uint32_t sq, uint32_t rq)
{
	qp->sq_cons = sq;
	qp->rq_cons = rq;
	REL(&qp->rec->sq_cons, sq);
	REL(&qp->rec->rq_cons, rq);
	REL(&qp->rec->sq_done, sq);
	REL(&qp->rec->rq_done, rq);
}

/* ---------------------------------------------------------------- */
/* copying                                                          */

/* The worker checks one inline length for SEND and RDMA WRITE alike. */
_Static_assert(offsetof(struct shmrdma_sq_wqe, send.inline_len) ==
	       offsetof(struct shmrdma_sq_wqe, rdma.inline_len),
	       "inline_len must be one field for SEND and RDMA WRITE");

/* SGEs of a receive entry that are both in range and inside the entry. */
static uint32_t rq_nsge(const struct qp *qp, const struct shmrdma_rq_wqe *rw)
{
	uint32_t fit = (qp->rq.esz - sizeof(*rw)) / sizeof(rw->sge[0]);
	uint32_t n = RLX(&rw->num_sge);

	if (n > fit)
		n = fit;
	return n > SHMRDMA_MAX_SGE ? SHMRDMA_MAX_SGE : n;
}

static inline void copy_bytes(uint8_t *dst, const uint8_t *src, uint64_t n,
			      bool nt)
{
	if (nt && plat_have_nt && n >= 512)
		plat_copy_nt(dst, src, n);
	else
		memcpy(dst, src, n);
}

/* ---------------------------------------------------------------- */
/* scatter/gather iteration                                         */

struct sg_iter {
	struct qp *qp;		/* whose device and PD the keys are in */
	const struct shmrdma_sge *sge;
	int nsge;
	int idx;
	uint64_t off;		/* within sge[idx] */
	bool remote;		/* sge is (iova, len, rkey) on a remote MR */
	uint32_t need_access;
	int err;		/* WC_* */
};

static struct mr *dev_mr(struct vdev *d, uint32_t key)
{
	uint32_t idx = SHMRDMA_KEY_INDEX(key);
	struct mr *mr;

	if (idx >= SHMRDMA_MAX_MR || !d->mrs)
		return NULL;
	mr = d->mrs[idx];
	if (!mr || !ACQ(&mr->valid) || mr->key != key)
		return NULL;
	return mr;
}

/*
 * The next contiguous host chunk, at most max bytes.  Returns length,
 * or -1 on error (it->err set), running out of SGEs included.
 */
static int64_t sg_next(struct sg_iter *it, uint64_t max, uint8_t **out)
{
	while (it->idx < it->nsge && it->off >= it->sge[it->idx].length) {
		it->idx++;
		it->off = 0;
	}
	if (it->idx >= it->nsge) {
		it->err = it->remote ? WC_REM_ACCESS_ERR : WC_LOC_LEN_ERR;
		return -1;
	}

	const struct shmrdma_sge *s = &it->sge[it->idx];
	uint64_t left = s->length - it->off;
	uint64_t addr = s->addr + it->off;
	uint64_t gpa, avail;
	uint8_t *p;

	if (left > max)
		left = max;

	struct vdev *d = it->qp->dev;

	if (s->lkey == SHMRDMA_LOCAL_DMA_LKEY && !it->remote) {
		gpa = addr;
		p = gpa_hva(d, gpa, &avail);
		if (!p) {
			it->err = WC_LOC_PROT_ERR;
			return -1;
		}
	} else {
		plat_spin_lock(&d->mr_lock);
		struct mr *mr = dev_mr(d, s->lkey);

		if (!mr || mr->pdn != it->qp->pdn ||
		    (mr->access & it->need_access) != it->need_access ||
		    addr < mr->iova || addr - mr->iova >= mr->length ||
		    mr->length - (addr - mr->iova) < left) {
			plat_spin_unlock(&d->mr_lock);
			it->err = it->remote ? WC_REM_ACCESS_ERR :
					       WC_LOC_PROT_ERR;
			return -1;
		}
		uint64_t moff = (addr - mr->iova) + mr->fpo;
		uint64_t pidx = moff >> mr->page_shift;
		uint64_t poff = moff & ((1ull << mr->page_shift) - 1);

		if (pidx >= mr->npages) {
			plat_spin_unlock(&d->mr_lock);
			it->err = it->remote ? WC_REM_ACCESS_ERR :
					       WC_LOC_PROT_ERR;
			return -1;
		}
		gpa = (mr->pages[pidx] & ~((1ull << mr->page_shift) - 1)) + poff;
		avail = (1ull << mr->page_shift) - poff;
		/* extend over physically contiguous pages */
		while (avail < left && pidx + 1 < mr->npages &&
		       (mr->pages[pidx + 1] & ~((1ull << mr->page_shift) - 1)) ==
		       (mr->pages[pidx] & ~((1ull << mr->page_shift) - 1)) +
		       (1ull << mr->page_shift)) {
			pidx++;
			avail += 1ull << mr->page_shift;
		}
		plat_spin_unlock(&d->mr_lock);
		uint64_t ravail;

		p = gpa_hva(d, gpa, &ravail);
		if (!p) {
			it->err = it->remote ? WC_REM_ACCESS_ERR :
					       WC_LOC_PROT_ERR;
			return -1;
		}
		if (ravail < avail)
			avail = ravail;
	}
	if (left > avail)
		left = avail;
	it->off += left;
	*out = p;
	return (int64_t)left;
}

static uint64_t sg_total(const struct shmrdma_sge *s, int n)
{
	uint64_t t = 0;

	for (int i = 0; i < n; i++)
		t += s[i].length;
	return t;
}

/*
 * Copy len bytes from src iterator to dst iterator.  Returns 0, or -1
 * with the failing side's err set.
 */
static int sg_copy_nt(struct sg_iter *dst, struct sg_iter *src, uint64_t len,
		      bool nt)
{
	uint8_t *sp = NULL, *dp = NULL;
	int64_t sl = 0, dl = 0;

	while (len) {
		if (!sl) {
			sl = sg_next(src, len, &sp);
			if (sl < 0)
				return -1;
		}
		if (!dl) {
			dl = sg_next(dst, len, &dp);
			if (dl < 0)
				return -1;
		}
		uint64_t n = sl < dl ? sl : dl;

		copy_bytes(dp, sp, n, nt);
		sp += n;
		dp += n;
		sl -= n;
		dl -= n;
		len -= n;
	}
	/* give back what we took but did not use */
	if (sl)
		src->off -= sl;
	if (dl)
		dst->off -= dl;
	if (nt)
		plat_nt_fence();
	return 0;
}

static int sg_copy(struct sg_iter *dst, struct sg_iter *src, uint64_t len)
{
	return sg_copy_nt(dst, src, len, false);
}

/* copy from a flat host buffer into an iterator */
static int sg_copy_in(struct sg_iter *dst, const uint8_t *src, uint64_t len)
{
	while (len) {
		uint8_t *dp;
		int64_t dl = sg_next(dst, len, &dp);

		if (dl < 0)
			return -1;
		memcpy(dp, src, dl);
		src += dl;
		len -= dl;
	}
	return 0;
}

/* advance an iterator by n bytes without touching memory */
static void sg_skip(struct sg_iter *it, uint64_t n)
{
	while (n && it->idx < it->nsge) {
		uint64_t rem = it->sge[it->idx].length - it->off;

		if (n < rem) {
			it->off += n;
			return;
		}
		n -= rem;
		it->idx++;
		it->off = 0;
	}
}

/*
 * Large copies are split into parts that helper threads run in
 * parallel with the worker that submitted them.  The worker holds its
 * mutex until every part is done, so teardown (wlock) cannot unmap
 * guest memory under a helper.
 */
struct copy_span {
	uint8_t *dst;
	const uint8_t *src;
	uint64_t len;
};

struct copy_job {
	struct sg_iter dst, src;	/* positioned at the start */
	const struct copy_span *spans;	/* instead of dst and src */
	int nspans;
	uint64_t len;
	uint32_t nparts;
	uint32_t next;			/* next part to claim */
	uint32_t done;			/* parts finished */
	int src_err, dst_err;
	struct copy_job *qnext;
};

static pthread_mutex_t g_cp_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t g_cp_cond = PTHREAD_COND_INITIALIZER;
static struct copy_job *g_cp_queue;
static int g_cp_sleepers;
static int g_cp_spinning;	/* copiers polling g_cp_queue */

static void part_range(struct copy_job *j, uint32_t i, uint64_t *off,
		       uint64_t *len)
{
	uint64_t per = (j->len / j->nparts) & ~4095ull;
	uint64_t start = per * i;

	*off = start;
	*len = (i == j->nparts - 1) ? j->len - start : per;
}

/* bytes [off, off + len) of a span list */
static void run_spans(struct copy_job *j, uint64_t off, uint64_t len)
{
	bool nt = j->len >= g_net_nt_min;
	int k = 0;

	while (k < j->nspans && off >= j->spans[k].len)
		off -= j->spans[k++].len;
	for (; len && k < j->nspans; k++, off = 0) {
		uint64_t n = j->spans[k].len - off;

		if (n > len)
			n = len;
		copy_bytes(j->spans[k].dst + off, j->spans[k].src + off, n,
			   nt);
		len -= n;
	}
	if (nt)
		plat_nt_fence();
}

static void run_part(struct copy_job *j, uint32_t i)
{
	struct sg_iter d = j->dst, s = j->src;
	uint64_t off, len;

	part_range(j, i, &off, &len);
	if (j->spans) {
		run_spans(j, off, len);
		__atomic_add_fetch(&j->done, 1, __ATOMIC_RELEASE);
		return;
	}
	sg_skip(&d, off);
	sg_skip(&s, off);
	if (sg_copy_nt(&d, &s, len, j->len >= g_nt_min)) {
		if (s.err)
			__atomic_store_n(&j->src_err, s.err, __ATOMIC_RELAXED);
		if (d.err)
			__atomic_store_n(&j->dst_err, d.err, __ATOMIC_RELAXED);
	}
	__atomic_add_fetch(&j->done, 1, __ATOMIC_RELEASE);
}

/* claim a part of a queued job; caller holds g_cp_lock */
static struct copy_job *cp_claim(uint32_t *part)
{
	for (struct copy_job **pp = &g_cp_queue; *pp; ) {
		struct copy_job *j = *pp;

		if (j->next < j->nparts) {
			*part = j->next++;
			if (j->next == j->nparts)
				*pp = j->qnext;	/* fully claimed */
			return j;
		}
		*pp = j->qnext;
	}
	return NULL;
}

static void *copier_main(void *arg)
{
	(void)arg;
	for (;;) {
		struct copy_job *j;
		uint32_t part;
		uint64_t t0 = now_ns();

		/* spin a little before sleeping: jobs come in bursts */
		__atomic_add_fetch(&g_cp_spinning, 1, __ATOMIC_SEQ_CST);
		while (!ACQ(&g_cp_queue) && now_ns() - t0 < 50000)
			cpu_relax();
		__atomic_sub_fetch(&g_cp_spinning, 1, __ATOMIC_SEQ_CST);
		pthread_mutex_lock(&g_cp_lock);
		while (!(j = cp_claim(&part))) {
			g_cp_sleepers++;
			pthread_cond_wait(&g_cp_cond, &g_cp_lock);
			g_cp_sleepers--;
		}
		pthread_mutex_unlock(&g_cp_lock);
		run_part(j, part);
	}
	return NULL;
}

/* run j's parts on the helpers and this thread; returns when all are done */
static void cp_run(struct copy_job *j)
{
	uint32_t part;

	pthread_mutex_lock(&g_cp_lock);
	j->qnext = g_cp_queue;
	g_cp_queue = j;
	j->next = 1;		/* part 0 is ours */
	if (j->spans) {
		/* one per frame: wake only the helpers the spinning ones
		 * do not cover; this thread runs any part left unclaimed */
		int wake = (int)j->nparts - 1 - RLX(&g_cp_spinning);

		if (wake > g_cp_sleepers)
			wake = g_cp_sleepers;
		while (wake-- > 0)
			pthread_cond_signal(&g_cp_cond);
	} else if (g_cp_sleepers) {
		pthread_cond_broadcast(&g_cp_cond);
	}
	pthread_mutex_unlock(&g_cp_lock);
	run_part(j, 0);
	/* help with our own job, then wait for the rest */
	for (;;) {
		pthread_mutex_lock(&g_cp_lock);
		if (j->next < j->nparts) {
			part = j->next++;
			if (j->next == j->nparts) {
				for (struct copy_job **pp = &g_cp_queue; *pp;
				     pp = &(*pp)->qnext)
					if (*pp == j) {
						*pp = j->qnext;
						break;
					}
			}
			pthread_mutex_unlock(&g_cp_lock);
			run_part(j, part);
			continue;
		}
		pthread_mutex_unlock(&g_cp_lock);
		break;
	}
	while (ACQ(&j->done) < j->nparts)
		cpu_relax();
}

/* copy, splitting across the helpers when it is large */
static int sg_copy_par(struct sg_iter *dst, struct sg_iter *src, uint64_t len)
{
	if (!g_ncopiers || len < g_split_min)
		return sg_copy_nt(dst, src, len, len >= g_nt_min);

	struct copy_job j = { .dst = *dst, .src = *src, .len = len };
	uint32_t n = len / g_part_min;

	if (n > (uint32_t)g_ncopiers + 1)
		n = g_ncopiers + 1;
	if (n < 2)
		return sg_copy_nt(dst, src, len, len >= g_nt_min);
	j.nparts = n;
	cp_run(&j);
	if (j.src_err || j.dst_err) {
		src->err = j.src_err;
		dst->err = j.dst_err;
		return -1;
	}
	sg_skip(dst, len);
	sg_skip(src, len);
	return 0;
}

/* ---------------------------------------------------------------- */
/* QP execution                                                     */

static struct vdev *dev_by_gid(const uint8_t *gid)
{
	for (struct vdev *d = g_devs; d; d = d->next) {
		if (d->dead || !d->initialized)
			continue;
		for (int i = 0; i < SHMRDMA_MAX_GID; i++)
			if (d->gid_valid[i] && !memcmp(d->gids[i], gid, 16))
				return d;
	}
	return NULL;
}

static bool dev_has_gid(const struct vdev *d, const uint8_t *gid)
{
	for (int i = 0; i < SHMRDMA_MAX_GID; i++)
		if (d->gid_valid[i] && !memcmp(d->gids[i], gid, 16))
			return true;
	return false;
}

static struct qp *qp_peer(struct qp *qp)
{
	uint64_t gen = ACQ(&g_gen);

	if (qp->peer_gen == gen)
		return qp->peer;
	qp->peer = NULL;
	qp->peer_gen = gen;
	struct vdev *pd = dev_by_gid(qp->dgid);

	if (!pd || qp->dqpn >= SHMRDMA_MAX_QP)
		return NULL;
	struct qp *p = pd->qps[qp->dqpn];

	/* QPNs are the guests' to choose: a peer must also be connected
	 * back to one of this device's GIDs, not just name our QPN. */
	if (!p || dev_by_gid(p->dgid) != qp->dev)
		return NULL;
	qp->peer = p;
	return p;
}

/* only qp's own worker, or a wlock holder; others use qp_err_req() */
static void qp_to_err(struct qp *qp)
{
	if (qp->state != QPS_ERR) {
		REL(&qp->state, QPS_ERR);
		if (!qp->fatal_sent) {
			qp->fatal_sent = true;
			REL(&qp->rec->fatal, 1);
			dev_raise_eq(qp->dev, qp->qpn, SHMRDMA_EQE_QP_FATAL);
		}
	}
}

/* any worker: have qp's owner move it to error */
static void qp_err_req(struct qp *qp)
{
	struct worker *w = qp->w;

	__atomic_store_n(&qp->err_req, 1, __ATOMIC_SEQ_CST);
	if (w && __atomic_load_n(&w->sleeping, __ATOMIC_SEQ_CST))
		plat_notifier_signal(w->evfd_w);
}

static void qp_flush_rq(struct qp *qp)
{
	struct shmrdma_cqe c = {0};

	plat_spin_lock(&qp->rq_lock);
	uint32_t prod = ACQ(&qp->rec->rq_prod);

	/* more posted than the ring holds: nothing valid to complete */
	if (prod - qp->rq_cons > qp->rq.entries) {
		qp->rq_cons = prod;
		REL(&qp->rec->rq_cons, prod);
		REL(&qp->rec->rq_done, prod);
	}
	while (qp->rq_cons != prod) {
		struct shmrdma_rq_wqe *w = ring_entry(&qp->rq, qp->rq_cons);

		c.wr_id = w->wr_id;
		c.qpn = qp->qpn;
		c.opcode = SHMRDMA_WC_RECV;
		c.status = WC_WR_FLUSH_ERR;
		qp->rq_cons++;
		wr_retire(qp, false, qp->rq_cons, qp->rcq, &c, false);
	}
	plat_spin_unlock(&qp->rq_lock);
}

enum { EX_DONE, EX_STALL };

static bool peer_ok(struct qp *qp, struct qp *peer)
{
	uint32_t st = peer ? RLX(&peer->state) : QPS_ERR;

	return peer && !peer->dev->dead && (st == QPS_RTR || st == QPS_RTS) &&
	       peer->type == SHMRDMA_QPT_RC && peer->dqpn == qp->qpn;
}

/* a receive producer past the ring; caller holds qp->rq_lock */
static bool rq_bad(struct qp *qp, uint32_t prod)
{
	if (prod - qp->rq_cons <= qp->rq.entries)
		return false;
	GLOG(qp->dev, "dev%d qp%u: rq producer %u is %u past consumer, ring holds %u",
	    qp->dev->id, qp->qpn, prod, prod - qp->rq_cons, qp->rq.entries);
	qp_err_req(qp);
	return true;
}

/* build the 40-byte RoCEv2 header area a UD receive starts with */
static void make_grh(uint8_t *out, const uint8_t *sgid, const uint8_t *dgid,
		     uint32_t paylen)
{
	static const uint8_t v4pfx[12] = { 0,0,0,0,0,0,0,0,0,0,0xff,0xff };

	memset(out, 0, SHMRDMA_GRH_BYTES);
	if (!memcmp(sgid, v4pfx, 12) && !memcmp(dgid, v4pfx, 12)) {
		uint8_t *ip = out + 20;
		uint32_t tot = 20 + 8 + 12 + paylen + 4;
		uint32_t sum = 0;

		ip[0] = 0x45;
		ip[2] = tot >> 8;
		ip[3] = tot & 0xff;
		ip[6] = 0x40;	/* DF */
		ip[8] = 64;
		ip[9] = 17;
		memcpy(ip + 12, sgid + 12, 4);
		memcpy(ip + 16, dgid + 12, 4);
		for (int i = 0; i < 20; i += 2)
			sum += (ip[i] << 8) | ip[i + 1];
		while (sum >> 16)
			sum = (sum & 0xffff) + (sum >> 16);
		sum = ~sum & 0xffff;
		ip[10] = sum >> 8;
		ip[11] = sum & 0xff;
	} else {
		/* IPv6 header: version 6, payload len, next hdr UDP */
		uint32_t plen = 8 + 12 + paylen + 4;

		out[0] = 0x60;
		out[4] = plen >> 8;
		out[5] = plen & 0xff;
		out[6] = 17;
		out[7] = 64;
		memcpy(out + 8, sgid, 16);
		memcpy(out + 24, dgid, 16);
	}
}

static int exec_ud_send(struct qp *qp, struct shmrdma_sq_wqe *w,
			const struct shmrdma_sge *sge)
{
	struct vdev *pd = dev_by_gid(w->ud.dgid);
	struct qp *peer;

	/* a source GID that is not ours would forge another VM's MADs */
	if (!dev_has_gid(qp->dev, w->ud.sgid)) {
		DBG("dev%d qp%u: UD drop, source GID not the device's",
		    qp->dev->id, qp->qpn);
		return WC_SUCCESS;
	}
	if (!pd || pd->stopped || w->ud.dqpn >= SHMRDMA_MAX_QP)
		return WC_SUCCESS;	/* dropped, as on a wire */
	peer = pd->qps[w->ud.dqpn];
	if (!peer || peer->type == SHMRDMA_QPT_RC ||
	    (RLX(&peer->state) != QPS_RTR && RLX(&peer->state) != QPS_RTS))
		return WC_SUCCESS;
	if (peer->type != SHMRDMA_QPT_GSI && peer->qkey != w->ud.qkey)
		return WC_SUCCESS;

	uint64_t len = (w->flags & SHMRDMA_WQE_INLINE) ? w->send.inline_len :
			sg_total(sge, w->num_sge);
	struct shmrdma_cqe c = {0};
	int rc = WC_SUCCESS;

	plat_spin_lock(&peer->rq_lock);
	uint32_t prod = ACQ(&peer->rec->rq_prod);

	if (peer->rq_cons == prod || rq_bad(peer, prod)) {
		plat_spin_unlock(&peer->rq_lock);
		DBG("dev%d qp%u: UD drop, no recv on dev%d qp%u", qp->dev->id,
		    qp->qpn, pd->id, peer->qpn);
		return WC_SUCCESS;
	}
	struct shmrdma_rq_wqe *rw = ring_entry(&peer->rq, peer->rq_cons);
	struct shmrdma_sge rsge[SHMRDMA_MAX_SGE];
	uint32_t rn = rq_nsge(peer, rw);

	memcpy(rsge, rw->sge, rn * sizeof(rsge[0]));
	c.wr_id = rw->wr_id;
	c.qpn = peer->qpn;
	c.opcode = SHMRDMA_WC_RECV;
	c.src_qp = qp->qpn;
	c.flags = SHMRDMA_CQE_GRH;

	struct sg_iter dst = { .qp = peer, .sge = rsge, .nsge = rn,
			       .need_access = SHMRDMA_ACC_LOCAL_WRITE };
	uint8_t grh[SHMRDMA_GRH_BYTES];

	make_grh(grh, w->ud.sgid, w->ud.dgid, len);
	if (grh[20] == 0x45)
		c.flags |= SHMRDMA_CQE_IPV4;

	if (sg_total(rsge, rn) < len + SHMRDMA_GRH_BYTES) {
		c.status = WC_LOC_LEN_ERR;
	} else if (sg_copy_in(&dst, grh, SHMRDMA_GRH_BYTES)) {
		c.status = dst.err;
	} else if (w->flags & SHMRDMA_WQE_INLINE) {
		if (sg_copy_in(&dst, (const uint8_t *)w->sge, len))
			c.status = dst.err;
	} else {
		struct sg_iter src = { .qp = qp, .sge = sge,
				       .nsge = w->num_sge };

		if (sg_copy(&dst, &src, len)) {
			if (src.err) {
				rc = src.err;
				plat_spin_unlock(&peer->rq_lock);
				return rc;
			}
			c.status = dst.err;
		}
	}
	c.byte_len = len + SHMRDMA_GRH_BYTES;
	peer->rq_cons++;
	wr_retire(peer, false, peer->rq_cons, peer->rcq, &c,
		  w->flags & SHMRDMA_WQE_SOLICITED);
	plat_spin_unlock(&peer->rq_lock);
	return rc;
}

/*
 * Consume one receive WQE on peer for a SEND (data from sge) or an
 * RDMA WRITE with immediate (no data).  Returns WC status for the
 * sender, or -1 to stall (no receive posted).
 */
static int deliver_recv(struct qp *qp, struct qp *peer,
			struct shmrdma_sq_wqe *w,
			const struct shmrdma_sge *sge, bool with_data)
{
	struct shmrdma_cqe c = {0};
	int rc = WC_SUCCESS;
	uint64_t len = 0;

	plat_spin_lock(&peer->rq_lock);
	uint32_t prod = ACQ(&peer->rec->rq_prod);

	if (rq_bad(peer, prod)) {
		plat_spin_unlock(&peer->rq_lock);
		return WC_REM_OP_ERR;
	}
	if (peer->rq_cons == prod) {
		plat_spin_unlock(&peer->rq_lock);
		return -1;
	}
	struct shmrdma_rq_wqe *rw = ring_entry(&peer->rq, peer->rq_cons);
	struct shmrdma_sge rsge[SHMRDMA_MAX_SGE];
	uint32_t rn = rq_nsge(peer, rw);

	memcpy(rsge, rw->sge, rn * sizeof(rsge[0]));
	c.wr_id = rw->wr_id;
	c.qpn = peer->qpn;
	c.src_qp = qp->qpn;

	if (with_data) {
		len = (w->flags & SHMRDMA_WQE_INLINE) ? w->send.inline_len :
		      sg_total(sge, w->num_sge);
		c.opcode = SHMRDMA_WC_RECV;
		if (len > sg_total(rsge, rn)) {
			c.status = WC_LOC_LEN_ERR;
			rc = WC_REM_INV_REQ_ERR;
		} else {
			struct sg_iter dst = { .qp = peer, .sge = rsge,
					       .nsge = rn,
					       .need_access = SHMRDMA_ACC_LOCAL_WRITE };

			if (w->flags & SHMRDMA_WQE_INLINE) {
				if (sg_copy_in(&dst, (const uint8_t *)w->sge,
					       len)) {
					c.status = dst.err;
					rc = WC_REM_OP_ERR;
				}
			} else {
				struct sg_iter src = { .qp = qp,
						       .sge = sge,
						       .nsge = w->num_sge };

				if (sg_copy_par(&dst, &src, len)) {
					if (src.err) {
						/* local fault: peer unaffected */
						plat_spin_unlock(&peer->rq_lock);
						return src.err;
					}
					c.status = dst.err;
					rc = WC_REM_OP_ERR;
				}
			}
		}
	} else {
		c.opcode = SHMRDMA_WC_RECV_RDMA_IMM;
		len = w->rdma.inline_len;	/* bytes written, see caller */
	}
	c.byte_len = len;
	if (w->opcode == SHMRDMA_OP_SEND_IMM ||
	    w->opcode == SHMRDMA_OP_RDMA_WRITE_IMM) {
		c.flags |= SHMRDMA_CQE_IMM;
		c.ex = w->ex;
	} else if (w->opcode == SHMRDMA_OP_SEND_INV) {
		c.flags |= SHMRDMA_CQE_INV;
		c.ex = w->ex;
		plat_spin_lock(&peer->dev->mr_lock);
		struct mr *mr = dev_mr(peer->dev, w->ex);

		if (mr && mr->pdn == peer->pdn)
			REL(&mr->valid, false);
		plat_spin_unlock(&peer->dev->mr_lock);
	}
	peer->rq_cons++;
	wr_retire(peer, false, peer->rq_cons, peer->rcq, &c,
		  w->flags & SHMRDMA_WQE_SOLICITED);
	plat_spin_unlock(&peer->rq_lock);
	if (c.status != WC_SUCCESS)
		qp_err_req(peer);
	return rc;
}

/* 32 MB of MR page lists per device */
#define DEV_MR_PAGES	(1u << 22)

static int exec_reg_mr(struct qp *qp, struct shmrdma_sq_wqe *w)
{
	struct vdev *d = qp->dev;
	uint32_t idx = SHMRDMA_KEY_INDEX(w->reg.key);
	struct mr *mr;

	if (idx == 0 || idx >= SHMRDMA_MAX_MR || w->reg.npages > SHMRDMA_MAX_FRPL ||
	    w->reg.page_shift < 12 || w->reg.page_shift > 30)
		return WC_LOC_QP_OP_ERR;
	uint64_t *pl = gpa_range(d, w->reg.pages_gpa, w->reg.npages * 8ull);

	if (!pl)
		return WC_LOC_PROT_ERR;
	plat_spin_lock(&d->mr_lock);
	mr = d->mrs[idx];
	if (!mr) {
		mr = calloc(1, sizeof(*mr));
		if (!mr) {
			plat_spin_unlock(&d->mr_lock);
			return WC_GENERAL_ERR;
		}
		d->mrs[idx] = mr;
	}
	REL(&mr->valid, false);
	if (w->reg.npages > mr->cap) {
		/* a FastReg MR's page list is sized once, at its first use */
		uint32_t cap = w->reg.npages < 64 ? 64 : w->reg.npages;
		uint64_t *np;

		if (cap - mr->cap > DEV_MR_PAGES - d->mr_pages) {
			plat_spin_unlock(&d->mr_lock);
			GLOG(d, "dev%d qp%u: MR page lists past %u entries, refused",
			    d->id, qp->qpn, DEV_MR_PAGES);
			return WC_LOC_QP_OP_ERR;
		}
		np = realloc(mr->pages, cap * sizeof(*np));
		if (!np) {
			plat_spin_unlock(&d->mr_lock);
			return WC_GENERAL_ERR;
		}
		d->mr_pages += cap - mr->cap;
		mr->pages = np;
		mr->cap = cap;
	}
	mr->key = w->reg.key;
	mr->iova = w->reg.iova;
	mr->length = w->reg.length;
	mr->page_shift = w->reg.page_shift;
	mr->npages = w->reg.npages;
	mr->access = w->reg.access;
	mr->pdn = qp->pdn;
	mr->fpo = w->ex & ((1u << w->reg.page_shift) - 1);
	if (w->reg.npages)
		memcpy(mr->pages, pl, w->reg.npages * 8ull);
	REL(&mr->valid, true);
	plat_spin_unlock(&d->mr_lock);
	return WC_SUCCESS;
}

static bool op_takes_inline(uint32_t op)
{
	return op == SHMRDMA_OP_SEND || op == SHMRDMA_OP_SEND_IMM ||
	       op == SHMRDMA_OP_SEND_INV || op == SHMRDMA_OP_RDMA_WRITE ||
	       op == SHMRDMA_OP_RDMA_WRITE_IMM;
}

static bool msg_too_long(const struct shmrdma_sq_wqe *w,
			 const struct shmrdma_sge *sge)
{
	return !(w->flags & SHMRDMA_WQE_INLINE) &&
	       sg_total(sge, w->num_sge) > SHMRDMA_MAX_MSG;
}

static int exec_wqe(struct qp *qp, struct shmrdma_sq_wqe *w,
		    const struct shmrdma_sge *sge, uint32_t *opc)
{
	struct qp *peer;
	int rc;

	switch (w->opcode) {
	case SHMRDMA_OP_SEND:
	case SHMRDMA_OP_SEND_IMM:
	case SHMRDMA_OP_SEND_INV:
		*opc = SHMRDMA_WC_SEND;
		if (msg_too_long(w, sge))
			return WC_LOC_LEN_ERR;
		if (qp->type != SHMRDMA_QPT_RC)
			return exec_ud_send(qp, w, sge);
		peer = qp_peer(qp);
		if (!peer_ok(qp, peer))
			return WC_RETRY_EXC_ERR;
		if (peer->dev->stopped)
			return -1;
		rc = deliver_recv(qp, peer, w, sge, true);
		if (rc < 0)
			return -1;
		if (rc == WC_SUCCESS)
			qp->tx_bytes += (w->flags & SHMRDMA_WQE_INLINE) ?
				w->send.inline_len : sg_total(sge, w->num_sge);
		return rc;

	case SHMRDMA_OP_RDMA_WRITE:
	case SHMRDMA_OP_RDMA_WRITE_IMM: {
		*opc = SHMRDMA_WC_RDMA_WRITE;
		if (qp->type != SHMRDMA_QPT_RC)
			return WC_LOC_QP_OP_ERR;
		if (msg_too_long(w, sge))
			return WC_LOC_LEN_ERR;
		peer = qp_peer(qp);
		if (!peer_ok(qp, peer))
			return WC_RETRY_EXC_ERR;
		if (peer->dev->stopped)
			return -1;
		if (!(peer->access & SHMRDMA_ACC_REMOTE_WRITE))
			return WC_REM_ACCESS_ERR;
		uint64_t len = (w->flags & SHMRDMA_WQE_INLINE) ?
			w->rdma.inline_len : sg_total(sge, w->num_sge);

		if (w->opcode == SHMRDMA_OP_RDMA_WRITE_IMM) {
			/* need a receive before moving data: check first */
			uint32_t prod = ACQ(&peer->rec->rq_prod);

			if (RLX(&peer->rq_cons) == prod)
				return -1;
		}
		struct shmrdma_sge rs = { .addr = w->rdma.remote_addr,
					  .length = len, .lkey = w->rdma.rkey };
		struct sg_iter dst = { .qp = peer, .sge = &rs, .nsge = 1,
				       .remote = true,
				       .need_access = SHMRDMA_ACC_REMOTE_WRITE };

		if (len) {
			if (w->flags & SHMRDMA_WQE_INLINE) {
				if (sg_copy_in(&dst, (const uint8_t *)w->sge, len))
					return dst.err;
			} else {
				struct sg_iter src = { .qp = qp, .sge = sge,
						       .nsge = w->num_sge };

				if (sg_copy_par(&dst, &src, len))
					return src.err ? src.err : dst.err;
			}
		}
		qp->tx_bytes += len;
		if (w->opcode == SHMRDMA_OP_RDMA_WRITE_IMM) {
			w->rdma.inline_len = len;
			rc = deliver_recv(qp, peer, w, NULL, false);
			return rc < 0 ? WC_RNR_RETRY_EXC_ERR : rc;
		}
		return WC_SUCCESS;
	}

	case SHMRDMA_OP_RDMA_READ: {
		*opc = SHMRDMA_WC_RDMA_READ;
		if (qp->type != SHMRDMA_QPT_RC)
			return WC_LOC_QP_OP_ERR;
		if (msg_too_long(w, sge))
			return WC_LOC_LEN_ERR;
		peer = qp_peer(qp);
		if (!peer_ok(qp, peer))
			return WC_RETRY_EXC_ERR;
		if (peer->dev->stopped)
			return -1;
		if (!(peer->access & SHMRDMA_ACC_REMOTE_READ))
			return WC_REM_ACCESS_ERR;
		uint64_t len = sg_total(sge, w->num_sge);
		struct shmrdma_sge rs = { .addr = w->rdma.remote_addr,
					  .length = len, .lkey = w->rdma.rkey };
		struct sg_iter src = { .qp = peer, .sge = &rs, .nsge = 1,
				       .remote = true,
				       .need_access = SHMRDMA_ACC_REMOTE_READ };
		struct sg_iter dst = { .qp = qp, .sge = sge,
				       .nsge = w->num_sge,
				       .need_access = SHMRDMA_ACC_LOCAL_WRITE };

		if (len && sg_copy_par(&dst, &src, len))
			return dst.err ? dst.err : src.err;
		qp->tx_bytes += len;
		return WC_SUCCESS;
	}

	case SHMRDMA_OP_REG_MR:
		*opc = SHMRDMA_WC_REG_MR;
		return exec_reg_mr(qp, w);

	case SHMRDMA_OP_LOCAL_INV: {
		*opc = SHMRDMA_WC_LOCAL_INV;
		uint32_t idx = SHMRDMA_KEY_INDEX(w->ex);
		struct mr *mr;

		if (idx >= SHMRDMA_MAX_MR)
			return WC_LOC_PROT_ERR;
		plat_spin_lock(&qp->dev->mr_lock);
		mr = qp->dev->mrs[idx];
		if (mr && mr->key == w->ex && mr->pdn != qp->pdn) {
			plat_spin_unlock(&qp->dev->mr_lock);
			return WC_LOC_PROT_ERR;
		}
		if (mr && mr->key == w->ex)
			REL(&mr->valid, false);
		plat_spin_unlock(&qp->dev->mr_lock);
		return WC_SUCCESS;
	}
	}
	*opc = SHMRDMA_WC_SEND;
	return WC_LOC_QP_OP_ERR;
}

/* bytes one pass moves for a QP, after its first WQE */
#define QP_PASS_BYTES	(8u << 20)

/*
 * Process up to budget send WQEs, fewer once QP_PASS_BYTES have moved or
 * wlock() is waiting; returns work done.
 */
static int qp_process(struct qp *qp, int budget, bool *stalled)
{
	uint64_t bytes0 = qp->tx_bytes;
	int done = 0;

	if (qp->dev->dead || qp->dev->stopped)
		return 0;
	if (RLX(&qp->err_req) && __atomic_exchange_n(&qp->err_req, 0,
						     __ATOMIC_SEQ_CST))
		qp_to_err(qp);

	if (qp->state == QPS_ERR) {
		uint32_t rp = ACQ(&qp->rec->rq_prod);

		if (rp != qp->rq_cons) {
			qp_flush_rq(qp);
			done++;
		}
	}
	if (qp->state != QPS_RTS && qp->state != QPS_ERR &&
	    qp->state != QPS_SQD && qp->state != QPS_SQE)
		return done;

	uint32_t prod = ACQ(&qp->rec->sq_prod);

	if (prod - qp->sq_cons > qp->sq.entries) {
		GLOG(qp->dev, "dev%d qp%u: sq producer %u is %u past consumer, ring holds %u",
		    qp->dev->id, qp->qpn, prod, prod - qp->sq_cons,
		    qp->sq.entries);
		qp_to_err(qp);
		qp->sq_cons = prod;
		REL(&qp->rec->sq_cons, prod);
		REL(&qp->rec->sq_done, prod);
		return done;
	}
	while (qp->sq_cons != prod && done < budget) {
		struct shmrdma_sq_wqe *gw = ring_entry(&qp->sq, qp->sq_cons);
		uint8_t buf[4096] __attribute__((aligned(16)));
		struct shmrdma_sq_wqe *w = (struct shmrdma_sq_wqe *)buf;
		struct shmrdma_cqe c = {0};
		uint32_t opc = SHMRDMA_WC_SEND;
		int st;

		memcpy(w, gw, qp->sq.esz);
		if (w->num_sge > (qp->sq.esz - SHMRDMA_WQE_HDR) / 16)
			w->num_sge = (qp->sq.esz - SHMRDMA_WQE_HDR) / 16;

		if (qp->state == QPS_ERR) {
			qp->stall_since = 0;
			st = WC_WR_FLUSH_ERR;
			switch (w->opcode) {
			case SHMRDMA_OP_RDMA_WRITE:
			case SHMRDMA_OP_RDMA_WRITE_IMM:
				opc = SHMRDMA_WC_RDMA_WRITE; break;
			case SHMRDMA_OP_RDMA_READ:
				opc = SHMRDMA_WC_RDMA_READ; break;
			case SHMRDMA_OP_REG_MR:
				opc = SHMRDMA_WC_REG_MR; break;
			case SHMRDMA_OP_LOCAL_INV:
				opc = SHMRDMA_WC_LOCAL_INV; break;
			}
		} else if ((w->flags & SHMRDMA_WQE_INLINE) &&
			   (qp->type != SHMRDMA_QPT_RC ||
			    !op_takes_inline(w->opcode))) {
			/* a UD WQE's address fields hold the inline length */
			st = WC_LOC_QP_OP_ERR;
		} else if ((w->flags & SHMRDMA_WQE_INLINE) &&
			   w->send.inline_len > qp->sq.esz - SHMRDMA_WQE_HDR) {
			st = WC_LOC_LEN_ERR;
		} else {
			st = exec_wqe(qp, w, w->sge, &opc);
			if (st < 0) {
				uint64_t t = now_ns();

				if (!qp->stall_since)
					qp->stall_since = t;
				if (qp->rnr_retry == 7 ||
				    t - qp->stall_since < g_rnr_timeout_ns) {
					*stalled = true;
					break;
				}
				st = WC_RNR_RETRY_EXC_ERR;
			}
			qp->stall_since = 0;
		}
		bool cqe = st != WC_SUCCESS ||
			   (w->flags & SHMRDMA_WQE_SIGNALED);

		c.wr_id = w->wr_id;
		c.qpn = qp->qpn;
		c.opcode = opc;
		c.status = st;
		qp->tx_wr++;
		qp->sq_cons++;
		wr_retire(qp, true, qp->sq_cons, qp->scq, cqe ? &c : NULL,
			  false);
		if (st != WC_SUCCESS && st != WC_WR_FLUSH_ERR) {
			GLOG(qp->dev, "dev%d qp%u: wqe op %u failed status %d",
			    qp->dev->id, qp->qpn, w->opcode, st);
			qp_to_err(qp);
		}
		done++;
		if (qp->tx_bytes - bytes0 >= QP_PASS_BYTES ||
		    __atomic_load_n(&g_wwait, __ATOMIC_SEQ_CST))
			break;
	}
	return done;
}

/* work qp_process() would do now; a stalled QP waits for the poll timeout */
static bool qp_has_work(struct qp *qp)
{
	if (qp->dev->dead || qp->dev->stopped)
		return false;
	if (__atomic_load_n(&qp->err_req, __ATOMIC_SEQ_CST))
		return true;
	if (qp->state == QPS_ERR && ACQ(&qp->rec->rq_prod) != qp->rq_cons)
		return true;
	if ((qp->state != QPS_RTS && qp->state != QPS_ERR &&
	     qp->state != QPS_SQD && qp->state != QPS_SQE) || qp->stall_since)
		return false;
	return ACQ(&qp->rec->sq_prod) != qp->sq_cons;
}

#include "shmnet.c"

static void *worker_main(void *arg)
{
	struct worker *w = arg;
	uint64_t idle_since = now_ns();

	for (;;) {
		bool stalled = false;
		int did = 0;

		/* between queues, let a waiting wlock() in: the lists may
		 * change under it, which costs only a queue's turn */
		pthread_mutex_lock(&w->mtx);
		for (int i = 0; i < w->nqps; i++) {
			did += qp_process(w->qps[i], 32, &stalled);
			worker_let_in(w);
		}
		for (int i = 0; i < w->nnq; i++) {
			did += net_tx_process(w->nqs[i], 64, &stalled);
			worker_let_in(w);
		}
		pthread_mutex_unlock(&w->mtx);
		worker_yield();

		uint64_t t = now_ns();

		if (did) {
			idle_since = t;
			continue;
		}
		if (t - idle_since < g_spin_ns) {
			for (int i = 0; i < 64; i++)
				cpu_relax();
			continue;
		}

		/* going idle: ask guests to ring the doorbell */
		bool work = false;

		pthread_mutex_lock(&w->mtx);
		__atomic_store_n(&w->sleeping, 1, __ATOMIC_SEQ_CST);
		for (int i = 0; i < w->nqps; i++)
			if (!w->qps[i]->dev->stopped)
				REL(&w->qps[i]->rec->need_kick, 1);
		for (int i = 0; i < w->nnq; i++)
			net_set_notify(w->nqs[i], true);
		FENCE();
		for (int i = 0; i < w->nqps && !work; i++)
			work = qp_has_work(w->qps[i]);
		for (int i = 0; i < w->nnq && !work; i++)
			work = net_has_work(w->nqs[i]);
		pthread_mutex_unlock(&w->mtx);
		worker_yield();

		if (!work) {
			struct plat_event evs[16];
			int ne = plat_poller_wait(w->epfd, evs, 16,
						  stalled ? 1 : 1000);

			/* its own wake fd; doorbells are never read here */
			for (int i = 0; i < ne; i++)
				if ((int)(intptr_t)evs[i].tag == w->evfd)
					plat_notifier_drain(w->evfd);
		}
		__atomic_store_n(&w->sleeping, 0, __ATOMIC_SEQ_CST);
		pthread_mutex_lock(&w->mtx);
		for (int i = 0; i < w->nqps; i++)
			if (!w->qps[i]->dev->stopped)
				REL(&w->qps[i]->rec->need_kick, 0);
		for (int i = 0; i < w->nnq; i++)
			net_set_notify(w->nqs[i], false);
		pthread_mutex_unlock(&w->mtx);
		worker_yield();
		idle_since = now_ns();
	}
	return NULL;
}

/* caller holds wlock */
static int worker_add_qp(struct qp *qp)
{
	struct worker *best = &g_workers[0];

	for (int i = 1; i < g_nworkers; i++)
		if (g_workers[i].nqps + g_workers[i].nnq <
		    best->nqps + best->nnq)
			best = &g_workers[i];
	if (best->nqps == best->cap) {
		int cap = best->cap ? best->cap * 2 : 16;
		struct qp **qps = realloc(best->qps, cap * sizeof(*qps));

		if (!qps)
			return -ENOMEM;
		best->qps = qps;
		best->cap = cap;
	}
	best->qps[best->nqps++] = qp;
	worker_watch(best, qp->dev->vq[SHMRDMA_VQ_DOORBELL].kickfd);
	qp->w = best;
	REL(&qp->rec->need_kick, ACQ(&best->sleeping) ? 1 : 0);
	return 0;
}

static void worker_del_qp(struct qp *qp)
{
	struct worker *w = qp->w;

	bool other = false;

	for (int i = 0; i < w->nqps; i++) {
		if (w->qps[i] == qp) {
			w->qps[i] = w->qps[--w->nqps];
			break;
		}
	}
	for (int i = 0; i < w->nqps && !other; i++)
		other = w->qps[i]->dev == qp->dev;
	if (!other)
		worker_unwatch(w, qp->dev->vq[SHMRDMA_VQ_DOORBELL].kickfd);
	qp->w = NULL;
}

/* ---------------------------------------------------------------- */
/* control commands                                                 */

/* 4 GB of rings per device: the daemon holds a pointer per page */
#define DEV_RING_PAGES	(1u << 20)

static int ring_setup(struct vdev *d, struct ring *r,
		      const struct shmrdma_ring_desc *rd, uint32_t max_entries)
{
	if (!rd->entries || (rd->entries & (rd->entries - 1)) ||
	    rd->entries > max_entries ||
	    !rd->entry_size || (rd->entry_size & (rd->entry_size - 1)) ||
	    rd->entry_size > 4096 || (rd->pages_gpa & 7) ||
	    rd->npages != ((uint64_t)rd->entries * rd->entry_size + 4095) / 4096)
		return -EINVAL;
	uint64_t *pl = gpa_range(d, rd->pages_gpa, rd->npages * 8ull);

	if (!pl)
		return -EFAULT;
	if (rd->npages > DEV_RING_PAGES - d->ring_pages)
		return -ENOMEM;
	r->pages = calloc(rd->npages, sizeof(*r->pages));
	if (!r->pages)
		return -ENOMEM;
	for (uint32_t i = 0; i < rd->npages; i++) {
		uint64_t gpa = RLX(&pl[i]);

		r->pages[i] = gpa_range(d, gpa, 4096);
		if (!r->pages[i] || (gpa & 4095)) {
			free(r->pages);
			r->pages = NULL;
			return -EFAULT;
		}
	}
	r->entries = rd->entries;
	r->esz = rd->entry_size;
	r->npages = rd->npages;
	d->ring_pages += r->npages;
	return 0;
}

static void ring_free(struct vdev *d, struct ring *r)
{
	if (!r->pages)
		return;
	d->ring_pages -= r->npages;
	free(r->pages);
	r->pages = NULL;
}

static void qp_free(struct qp *qp)
{
	ring_free(qp->dev, &qp->sq);
	ring_free(qp->dev, &qp->rq);
	plat_spin_destroy(&qp->rq_lock);
	free(qp);
}

/* caller holds wlock */
static void dev_reset_state(struct vdev *d)
{
	for (int i = 0; i < SHMRDMA_MAX_QP; i++) {
		struct qp *qp = d->qps[i];

		if (!qp)
			continue;
		if (qp->w)
			worker_del_qp(qp);
		d->qps[i] = NULL;
		qp_free(qp);
	}
	for (int i = 0; i < SHMRDMA_MAX_CQ; i++) {
		struct cq *cq = d->cqs[i];

		if (!cq)
			continue;
		ring_free(d, &cq->ring);
		plat_spin_destroy(&cq->lock);
		free(cq);
		d->cqs[i] = NULL;
	}
	if (d->mrs) {
		for (int i = 0; i < SHMRDMA_MAX_MR; i++) {
			if (d->mrs[i])
				free(d->mrs[i]->pages);
			free(d->mrs[i]);
			d->mrs[i] = NULL;
		}
	}
	d->mr_pages = 0;
	memset(d->gid_valid, 0, sizeof(d->gid_valid));
	d->initialized = false;
	d->shared = NULL;
	d->eq = NULL;
	unmap_all(d->old, d->nold);
	d->nold = 0;
	d->eq_prod = 0;
	d->irq_pending = false;
	d->stopped = false;
	for (int i = 0; i < MAX_VQS; i++)
		d->vq[i].mark = false;
	__atomic_add_fetch(&g_gen, 1, __ATOMIC_SEQ_CST);
}

static void gid_str(const uint8_t *g, char *buf, size_t n)
{
	static const uint8_t v4pfx[12] = { 0,0,0,0,0,0,0,0,0,0,0xff,0xff };

	if (!memcmp(g, v4pfx, 12))
		snprintf(buf, n, "%u.%u.%u.%u", g[12], g[13], g[14], g[15]);
	else
		snprintf(buf, n, "%02x%02x:%02x%02x:...:%02x%02x:%02x%02x",
			 g[0], g[1], g[2], g[3], g[12], g[13], g[14], g[15]);
}

/* records hold atomics, which fault on aarch64 unless aligned */
#define REC_OK(gpa)	(!((gpa) & 7))

static int do_cmd(struct vdev *d, const struct shmrdma_cmd *c,
		  struct shmrdma_resp *r)
{
	char gs[64];

	switch (c->cmd) {
	case SHMRDMA_CMD_INIT: {
		r->abi = SHMRDMA_ABI_VERSION;
		if (c->init.abi != SHMRDMA_ABI_VERSION)
			return -EPROTO;
		uint32_t n = c->init.eq_entries;

		if (!n || (n & (n - 1)))
			return -EINVAL;
		void *sh = REC_OK(c->init.shared_gpa) ?
			   gpa_range(d, c->init.shared_gpa, sizeof(*d->shared)) : NULL;
		void *eq = gpa_range(d, c->init.eq_gpa, n * sizeof(struct shmrdma_eqe));

		if (!sh || !eq)
			return -EFAULT;
		struct mr **mrs = d->mrs ? NULL :
				  calloc(SHMRDMA_MAX_MR, sizeof(*mrs));

		if (!d->mrs && !mrs)
			return -ENOMEM;
		wlock();
		dev_reset_state(d);
		if (mrs)
			d->mrs = mrs;
		d->shared = sh;
		d->eq = eq;
		d->eq_entries = n;
		d->eq_prod = RLX(&d->shared->eq_cons);
		REL(&d->shared->eq_lost, 0);
		d->initialized = true;
		wunlock();
		r->val = (uint32_t)d->id | SHMRDMA_INIT_F_LIMBO;
		GLOG(d, "dev%d: initialized, eq %u entries", d->id, n);
		return 0;
	}
	case SHMRDMA_CMD_ADD_GID:
		if (!d->initialized || c->gid.index >= SHMRDMA_MAX_GID)
			return -EINVAL;
		wlock();
		/* a GID names one device: taking another's takes its traffic */
		for (struct vdev *o = g_devs; o; o = o->next) {
			if (o == d || o->dead || !o->initialized ||
			    !dev_has_gid(o, c->gid.gid))
				continue;
			wunlock();
			gid_str(c->gid.gid, gs, sizeof(gs));
			GLOG(d, "dev%d: gid %s is dev%d's, refused", d->id, gs, o->id);
			return -EEXIST;
		}
		memcpy(d->gids[c->gid.index], c->gid.gid, 16);
		d->gid_valid[c->gid.index] = true;
		__atomic_add_fetch(&g_gen, 1, __ATOMIC_SEQ_CST);
		wunlock();
		gid_str(c->gid.gid, gs, sizeof(gs));
		GLOG(d, "dev%d: gid[%u] = %s", d->id, c->gid.index, gs);
		return 0;
	case SHMRDMA_CMD_DEL_GID:
		if (c->gid.index >= SHMRDMA_MAX_GID)
			return -EINVAL;
		wlock();
		d->gid_valid[c->gid.index] = false;
		__atomic_add_fetch(&g_gen, 1, __ATOMIC_SEQ_CST);
		wunlock();
		return 0;
	case SHMRDMA_CMD_CREATE_CQ: {
		uint32_t cqn = c->create_cq.cqn;
		struct cq *cq;
		int rc;

		if (!d->initialized || cqn >= SHMRDMA_MAX_CQ || d->cqs[cqn])
			return -EINVAL;
		cq = calloc(1, sizeof(*cq));
		if (!cq)
			return -ENOMEM;
		cq->dev = d;
		cq->cqn = cqn;
		cq->rec = REC_OK(c->create_cq.rec_gpa) ?
			  gpa_range(d, c->create_cq.rec_gpa, sizeof(*cq->rec)) : NULL;
		if (!cq->rec) {
			free(cq);
			return -EFAULT;
		}
		rc = ring_setup(d, &cq->ring, &c->create_cq.ring, SHMRDMA_MAX_CQE);
		if (rc) {
			free(cq);
			return rc;
		}
		if (cq->ring.esz != sizeof(struct shmrdma_cqe)) {
			ring_free(d, &cq->ring);
			free(cq);
			return -EINVAL;
		}
		plat_spin_init(&cq->lock);
		cq->prod = RLX(&cq->rec->prod);
		wlock();
		d->cqs[cqn] = cq;
		wunlock();
		DBG("dev%d: cq%u %u entries", d->id, cqn, cq->ring.entries);
		return 0;
	}
	case SHMRDMA_CMD_DESTROY_CQ: {
		uint32_t cqn = c->destroy_cq.cqn;
		struct cq *cq;

		if (cqn >= SHMRDMA_MAX_CQ || !(cq = d->cqs[cqn]))
			return -EINVAL;
		wlock();
		for (int i = 0; i < SHMRDMA_MAX_QP; i++)
			if (d->qps[i] && (d->qps[i]->scq == cq ||
					  d->qps[i]->rcq == cq)) {
				wunlock();
				return -EBUSY;
			}
		d->cqs[cqn] = NULL;
		wunlock();
		ring_free(d, &cq->ring);
		plat_spin_destroy(&cq->lock);
		free(cq);
		return 0;
	}
	case SHMRDMA_CMD_CREATE_QP: {
		uint32_t qpn = c->create_qp.qpn;
		uint32_t sc = c->create_qp.send_cqn, rcn = c->create_qp.recv_cqn;
		struct qp *qp;
		int rc;

		if (!d->initialized || qpn >= SHMRDMA_MAX_QP || d->qps[qpn] ||
		    sc >= SHMRDMA_MAX_CQ || rcn >= SHMRDMA_MAX_CQ ||
		    !d->cqs[sc] || !d->cqs[rcn])
			return -EINVAL;
		qp = calloc(1, sizeof(*qp));
		if (!qp)
			return -ENOMEM;
		qp->dev = d;
		qp->qpn = qpn;
		qp->type = c->create_qp.type;
		qp->pdn = c->create_qp.pdn;
		qp->state = QPS_RESET;
		qp->scq = d->cqs[sc];
		qp->rcq = d->cqs[rcn];
		qp->rec = REC_OK(c->create_qp.rec_gpa) ?
			  gpa_range(d, c->create_qp.rec_gpa, sizeof(*qp->rec)) : NULL;
		if (!qp->rec) {
			free(qp);
			return -EFAULT;
		}
		rc = ring_setup(d, &qp->sq, &c->create_qp.sq, SHMRDMA_MAX_QP_WR);
		if (!rc)
			rc = ring_setup(d, &qp->rq, &c->create_qp.rq,
					SHMRDMA_MAX_QP_WR);
		if (rc || qp->sq.esz < SHMRDMA_WQE_HDR ||
		    qp->rq.esz < sizeof(struct shmrdma_rq_wqe)) {
			ring_free(d, &qp->sq);
			ring_free(d, &qp->rq);
			free(qp);
			return rc ? rc : -EINVAL;
		}
		plat_spin_init(&qp->rq_lock);
		qp_set_cons(qp, RLX(&qp->rec->sq_cons),
			    RLX(&qp->rec->rq_cons));
		qp->rnr_retry = 7;
		wlock();
		if (worker_add_qp(qp)) {
			wunlock();
			qp_free(qp);
			return -ENOMEM;
		}
		d->qps[qpn] = qp;
		__atomic_add_fetch(&g_gen, 1, __ATOMIC_SEQ_CST);
		wunlock();
		DBG("dev%d: qp%u type %u sq %u rq %u", d->id, qpn, qp->type,
		    qp->sq.entries, qp->rq.entries);
		return 0;
	}
	case SHMRDMA_CMD_MODIFY_QP: {
		uint32_t qpn = c->modify_qp.qpn;
		struct qp *qp;

		if (qpn >= SHMRDMA_MAX_QP || !(qp = d->qps[qpn]))
			return -EINVAL;
		wlock();
		/* other QPs' cached peers may name this one */
		if (c->modify_qp.mask & SHMRDMA_MQ_DEST) {
			memcpy(qp->dgid, c->modify_qp.dgid, 16);
			qp->dqpn = c->modify_qp.dest_qpn;
			__atomic_add_fetch(&g_gen, 1, __ATOMIC_SEQ_CST);
		}
		if (c->modify_qp.mask & SHMRDMA_MQ_QKEY)
			qp->qkey = c->modify_qp.qkey;
		if (c->modify_qp.mask & SHMRDMA_MQ_RETRY)
			qp->rnr_retry = c->modify_qp.rnr_retry;
		if (c->modify_qp.mask & SHMRDMA_MQ_ACCESS)
			qp->access = c->modify_qp.access;
		if (c->modify_qp.mask & SHMRDMA_MQ_STATE) {
			uint32_t ns = c->modify_qp.state;

			if (ns == QPS_RESET) {
				/* drop everything outstanding, no completions */
				qp_set_cons(qp, ACQ(&qp->rec->sq_prod),
					    ACQ(&qp->rec->rq_prod));
				qp->fatal_sent = false;
				REL(&qp->rec->fatal, 0);
				qp->err_req = 0;
			}
			qp->state = ns;
			qp->stall_since = 0;
			if (ns == QPS_ERR)
				qp_flush_rq(qp);
			__atomic_add_fetch(&g_gen, 1, __ATOMIC_SEQ_CST);
		}
		wunlock();
		gid_str(qp->dgid, gs, sizeof(gs));
		DBG("dev%d: qp%u -> state %u dest %s/%u", d->id, qpn, qp->state,
		    gs, qp->dqpn);
		wake_workers();
		return 0;
	}
	case SHMRDMA_CMD_DESTROY_QP: {
		uint32_t qpn = c->destroy_qp.qpn;
		struct qp *qp;

		if (qpn >= SHMRDMA_MAX_QP || !(qp = d->qps[qpn]))
			return -EINVAL;
		wlock();
		d->qps[qpn] = NULL;
		worker_del_qp(qp);
		__atomic_add_fetch(&g_gen, 1, __ATOMIC_SEQ_CST);
		wunlock();
		DBG("dev%d: qp%u destroyed, %" PRIu64 " bytes %" PRIu64 " wrs",
		    d->id, qpn, qp->tx_bytes, qp->tx_wr);
		qp_free(qp);
		return 0;
	}
	case SHMRDMA_CMD_DEREG_MR: {
		uint32_t idx = SHMRDMA_KEY_INDEX(c->dereg_mr.key);

		if (idx >= SHMRDMA_MAX_MR || !d->mrs)
			return -EINVAL;
		wlock();
		if (d->mrs[idx]) {
			d->mr_pages -= d->mrs[idx]->cap;
			free(d->mrs[idx]->pages);
		}
		free(d->mrs[idx]);
		d->mrs[idx] = NULL;
		wunlock();
		return 0;
	}
	case SHMRDMA_CMD_RESET:
		wlock();
		dev_reset_state(d);
		wunlock();
		return 0;
	}
	return -EOPNOTSUPP;
}

static void ctrl_process(struct vdev *d)
{
	struct vq *q = &d->vq[SHMRDMA_VQ_CTRL];
	bool any = false;
	uint16_t ai;

	if (d->stopped || !vq_ready(q) || q->broken)
		return;
	ai = ACQ(&q->avail->idx);
	if ((uint16_t)(ai - q->last_avail) > q->num) {
		GLOG(d, "dev%d: ctrl vq avail %u is past %u", d->id, ai,
		    q->last_avail);
		q->broken = true;
		return;
	}
	while (q->last_avail != ai) {
		uint16_t head = RLX(&q->avail->ring[q->last_avail % q->num]);
		struct shmrdma_cmd cmd;
		struct shmrdma_resp resp = {0};
		size_t got = 0;
		uint8_t *rp = NULL;
		uint32_t rl = 0;
		uint32_t i = head;
		bool bad = false;

		memset(&cmd, 0, sizeof(cmd));
		q->last_avail++;
		for (int guard = 0;; guard++) {
			if (i >= q->num || guard == 16) {
				bad = true;
				break;
			}

			struct vring_desc de = desc_load(&q->desc[i]);
			uint8_t *p = (de.flags & VRING_DESC_F_INDIRECT) ? NULL :
				     gpa_range(d, de.addr, de.len);

			if (!p) {
				bad = true;
				break;
			}
			if (de.flags & VRING_DESC_F_WRITE) {
				if (!rp) {
					rp = p;
					rl = de.len;
				}
			} else if (got < sizeof(cmd)) {
				size_t n = de.len;

				if (n > sizeof(cmd) - got)
					n = sizeof(cmd) - got;
				memcpy((uint8_t *)&cmd + got, p, n);
				got += n;
			}
			if (!(de.flags & VRING_DESC_F_NEXT))
				break;
			i = de.next;
		}
		if (bad) {
			/* the guest times out and resets the device */
			GLOG(d, "dev%d: ctrl vq chain at head %u is invalid", d->id,
			    head);
			q->broken = true;
			break;
		}
		resp.status = do_cmd(d, &cmd, &resp);
		if (resp.status)
			GLOG(d, "dev%d: cmd %u failed %d", d->id, cmd.cmd,
			    resp.status);
		if (rp && rl >= sizeof(resp))
			memcpy(rp, &resp, sizeof(resp));
		vq_push_used(q, head, rp ? sizeof(resp) : 0);
		any = true;
	}
	if (any)
		vq_signal(q);
}

/* ---------------------------------------------------------------- */
/* vhost-user message handling                                      */

/* t lives as long as its device, or for ever */
static int ep_add(int fd, struct ep_tag *t, int kind, struct vdev *d, int vq)
{
	t->kind = kind;
	t->d = d;
	t->vq = vq;
	if (plat_poller_add(g_epfd, fd, t, false)) {
		LOG("poller add %d: %s", fd, strerror(errno));
		return -1;
	}
	return 0;
}

static void ep_del(int fd)
{
	plat_poller_del(g_epfd, fd);
}

static void vq_stop(struct vdev *d, int idx)
{
	struct vq *q = &d->vq[idx];

	q->started = false;
	if (q->kickfd >= 0) {
		ep_del(q->kickfd);
		worker_unwatch_all(q->kickfd);
		close(q->kickfd);
		q->kickfd = -1;
	}
}

/* forget the ring as a front end's reset does; caller holds wlock */
static void vq_reset(struct vdev *d, int idx)
{
	struct vq *q = &d->vq[idx];

	vq_stop(d, idx);
	q->enabled = false;
	q->broken = false;
	q->desc = NULL;
	q->avail = NULL;
	q->used = NULL;
	q->desc_ua = q->avail_ua = q->used_ua = 0;
	q->num = 0;
	q->last_avail = q->used_idx = 0;
	q->signalled_valid = false;
}

static void dev_destroy(struct vdev *d)
{
	LOG("dev%d: disconnected", d->id);
	wlock();
	d->dead = true;
	dev_reset_state(d);
	if (d->port)
		net_port_free(d);
	for (struct vdev **pp = &g_devs; *pp; pp = &(*pp)->next)
		if (*pp == d) {
			*pp = d->next;
			break;
		}
	wunlock();
	for (int i = 0; i < MAX_VQS; i++) {
		vq_stop(d, i);
		if (d->vq[i].callfd >= 0)
			close(d->vq[i].callfd);
	}
	ep_del(d->sock);
	close(d->sock);
	unmap_all(d->map, d->nmap);
	unmap_all(d->old, d->nold);
	free(d->mrs);
	plat_spin_destroy(&d->mr_lock);
	plat_spin_destroy(&d->evq_lock);
	plat_spin_destroy(&d->log_lock);
	free(d);
}

static int vu_reply(struct vdev *d, uint32_t req, const void *p, uint32_t sz)
{
	struct {
		struct vu_hdr h;
		uint8_t b[256];
	} m;

	m.h.request = req;
	m.h.flags = VU_VERSION | VU_FLAG_REPLY;
	m.h.size = sz;
	memcpy(m.b, p, sz);
	return send(d->sock, &m, sizeof(m.h) + sz, PLAT_MSG_NOSIGNAL) < 0 ? -1 : 0;
}

/*
 * QEMU stops the vrings for a VM stop and for the guest's reset alike;
 * only a reset follows with RESET_DEVICE.  So a stop keeps the state,
 * and nothing may touch the guest's memory until the rings come back
 * exactly as they were.  Caller holds wlock.
 */
static void dev_stop(struct vdev *d, struct vq *q)
{
	if (!d->stopped) {
		d->stopped = true;
		for (int i = 0; i < MAX_VQS; i++)
			d->vq[i].mark = false;
	}
	q->mark = true;
	q->mark_num = q->num;
	q->mark_base = q->last_avail;
	q->mark_desc = q->desc_ua;
	q->mark_avail = q->avail_ua;
	q->mark_used = q->used_ua;
}

/* a stopped device's vring q is started again; caller holds wlock */
static void dev_restart(struct vdev *d, struct vq *q)
{
	if (q->mark && q->num == q->mark_num && q->last_avail == q->mark_base &&
	    q->desc_ua == q->mark_desc && q->avail_ua == q->mark_avail &&
	    q->used_ua == q->mark_used)
		return;
	GLOG(d, "dev%d: restarted with other vrings, state dropped", d->id);
	dev_reset_state(d);
}

/* carry on once every stopped vring is running again */
static void dev_resume(struct vdev *d)
{
	bool resumed = false;

	wlock();
	if (d->stopped) {
		resumed = true;
		for (int i = 0; i < MAX_VQS; i++)
			if (d->vq[i].mark && !vq_ready(&d->vq[i]))
				resumed = false;
		if (resumed) {
			d->stopped = false;
			for (int i = 0; i < MAX_VQS; i++)
				d->vq[i].mark = false;
		}
	}
	wunlock();
	if (resumed) {
		LOG("dev%d: resumed", d->id);
		plat_spin_lock(&d->evq_lock);
		if (d->irq_pending)
			dev_send_irq(d);
		plat_spin_unlock(&d->evq_lock);
		wake_workers_for(d);
	}
	ctrl_process(d);
}

static int vq_map(struct vdev *d, struct vq *q);

/* closes every fd it is given */
static int set_mem_table(struct vdev *d, struct vu_memtab *mt, int *fds, int nfds)
{
	struct mapping nm[VU_MAX_REGIONS];
	bool same;

	if (mt->nregions > VU_MAX_REGIONS || (int)mt->nregions != nfds) {
		LOG("dev%d: bad mem table (%u regions, %d fds)", d->id,
		    mt->nregions, nfds);
		for (int i = 0; i < nfds; i++)
			close(fds[i]);
		return -1;
	}
	/* QEMU resends the table on every restart: a VM stop and cont must
	 * not use up the old maps kept below */
	same = (int)mt->nregions == d->nmap;
	for (uint32_t i = 0; i < mt->nregions && same; i++) {
		struct vu_region *r = &mt->r[i];
		struct stat st;

		same = !fstat(fds[i], &st) && r->gpa == d->map[i].gpa &&
		       r->size == d->map[i].size &&
		       r->uaddr == d->map[i].uaddr &&
		       r->mmap_offset == d->map[i].offset &&
		       (uint64_t)st.st_dev == d->map[i].st_dev &&
		       (uint64_t)st.st_ino == d->map[i].st_ino;
	}
	if (same) {
		for (int i = 0; i < nfds; i++)
			close(fds[i]);
		return 0;
	}
	memset(nm, 0, sizeof(nm));
	for (uint32_t i = 0; i < mt->nregions; i++) {
		struct vu_region *r = &mt->r[i];
		size_t len = r->size + r->mmap_offset;
		struct stat st;
		void *p = MAP_FAILED;

		/* a mapping past the end of its file faults when touched */
		if (!r->size || r->size > SIZE_MAX - r->mmap_offset ||
		    fstat(fds[i], &st) || st.st_size < 0 ||
		    (uint64_t)st.st_size < len)
			errno = EINVAL;
		else
			p = mmap(NULL, len, PROT_READ | PROT_WRITE, MAP_SHARED,
				 fds[i], 0);
		close(fds[i]);
		if (p == MAP_FAILED) {
			LOG("dev%d: mmap region %u: %s", d->id, i,
			    strerror(errno));
			unmap_all(nm, i);
			for (uint32_t j = i + 1; j < mt->nregions; j++)
				close(fds[j]);
			return -1;
		}
		nm[i].gpa = r->gpa;
		nm[i].size = r->size;
		nm[i].uaddr = r->uaddr;
		nm[i].offset = r->mmap_offset;
		nm[i].st_dev = st.st_dev;
		nm[i].st_ino = st.st_ino;
		nm[i].map_base = p;
		nm[i].map_len = len;
		nm[i].hva = (uint8_t *)p + r->mmap_offset;
		LOG("dev%d: region %u gpa 0x%" PRIx64 " size 0x%" PRIx64,
		    d->id, i, r->gpa, r->size);
	}
	wlock();
	if (!d->initialized) {
		/* only the vrings point into the maps, and they move below */
		unmap_all(d->map, d->nmap);
	} else if (d->nold + d->nmap <= (int)(sizeof(d->old) / sizeof(d->old[0]))) {
		/* QPs, CQs and the EQ keep pointers into the current maps */
		memcpy(&d->old[d->nold], d->map, d->nmap * sizeof(d->map[0]));
		d->nold += d->nmap;
	} else {
		wunlock();
		LOG("dev%d: too many mem table changes while in use, refused",
		    d->id);
		unmap_all(nm, mt->nregions);
		return -1;
	}
	memcpy(d->map, nm, sizeof(nm));
	d->nmap = mt->nregions;
	/* before any worker runs again: the old maps may be gone */
	for (int i = 0; i < MAX_VQS; i++)
		if (d->vq[i].desc_ua)
			vq_map(d, &d->vq[i]);
	wunlock();
	return 0;
}

/* a vring that cannot be mapped whole is left with no pointers at all */
static int vq_map(struct vdev *d, struct vq *q)
{
	bool ok = q->num && !(q->desc_ua & 15) && !(q->avail_ua & 1) &&
		  !(q->used_ua & 3);

	q->desc = ok ? uaddr_hva(d, q->desc_ua, 16ull * q->num) : NULL;
	q->avail = ok ? uaddr_hva(d, q->avail_ua, 6 + 2ull * q->num) : NULL;
	q->used = ok ? uaddr_hva(d, q->used_ua, 6 + 8ull * q->num) : NULL;
	if (q->desc && q->avail && q->used)
		return 0;
	q->desc = NULL;
	q->avail = NULL;
	q->used = NULL;
	return -1;
}

static int handle_msg(struct vdev *d)
{
	struct vu_hdr h;
	union vu_payload *pl = calloc(1, sizeof(*pl));
	int fds[VU_MAX_REGIONS];
	int nfds = 0;
	char cbuf[CMSG_SPACE(sizeof(int) * VU_MAX_REGIONS)];
	struct iovec iov = { .iov_base = &h, .iov_len = sizeof(h) };
	struct msghdr mh = { .msg_iov = &iov, .msg_iovlen = 1,
			     .msg_control = cbuf, .msg_controllen = sizeof(cbuf) };
	ssize_t n;
	int rc = 0;

	if (!pl)
		return -1;
	n = plat_recvmsg(d->sock, &mh);
	if (n <= 0) {
		free(pl);
		return -1;
	}
	for (struct cmsghdr *cm = CMSG_FIRSTHDR(&mh); cm;
	     cm = CMSG_NXTHDR(&mh, cm)) {
		if (cm->cmsg_level == SOL_SOCKET && cm->cmsg_type == SCM_RIGHTS) {
			int k = (cm->cmsg_len - CMSG_LEN(0)) / sizeof(int);
			int *in = (int *)CMSG_DATA(cm);

			for (int i = 0; i < k; i++) {
				if (nfds < VU_MAX_REGIONS)
					fds[nfds++] = in[i];
				else
					close(in[i]);
			}
		}
	}
	/* the fds that did not fit are gone: the message cannot be obeyed */
	if (mh.msg_flags & MSG_CTRUNC) {
		LOG("dev%d: file descriptors lost (out of fds?)", d->id);
		rc = -1;
		goto out;
	}
	if (n != sizeof(h)) {
		LOG("dev%d: short header", d->id);
		rc = -1;
		goto out;
	}
	if (h.size > sizeof(*pl)) {
		LOG("dev%d: oversized message %u", d->id, h.size);
		rc = -1;
		goto out;
	}
	if (h.size) {
		size_t got = 0;

		while (got < h.size) {
			n = recv(d->sock, pl->raw + got, h.size - got, 0);
			if (n <= 0) {
				rc = -1;
				goto out;
			}
			got += n;
		}
	}

	DBG("dev%d: vhost-user req %u size %u fds %d", d->id, h.request,
	    h.size, nfds);

	bool need_ack = (h.flags & VU_FLAG_NEED_REPLY) &&
			(d->pfeatures & PF_REPLY_ACK);
	uint64_t ack = 0;

	switch (h.request) {
	case VU_GET_FEATURES: {
		uint64_t f = d->kind == KIND_NET ? NET_FEATURES :
			     F_VERSION_1 | F_PROTOCOL_FEATURES;

		rc = vu_reply(d, h.request, &f, 8);
		need_ack = false;
		break;
	}
	case VU_SET_FEATURES:
		d->features = pl->u64;
		for (int i = 0; i < MAX_VQS; i++)
			d->vq[i].event_idx = d->features & F_RING_EVENT_IDX;
		break;
	case VU_GET_PROTOCOL_FEATURES: {
		/* RESET_DEVICE tells a guest's reset from a VM stop */
		uint64_t f = PF_MQ | PF_REPLY_ACK |
			     (d->kind == KIND_RDMA ? PF_RESET_DEVICE : 0);

		rc = vu_reply(d, h.request, &f, 8);
		need_ack = false;
		break;
	}
	case VU_SET_PROTOCOL_FEATURES:
		d->pfeatures = pl->u64;
		break;
	case VU_GET_QUEUE_NUM: {
		uint64_t qn = d->kind == KIND_NET ? NET_MAX_QP :
			      SHMRDMA_NUM_VQS;

		rc = vu_reply(d, h.request, &qn, 8);
		need_ack = false;
		break;
	}
	case VU_SET_OWNER:
		break;
	case VU_RESET_OWNER:
	case VU_RESET_DEVICE:
		wlock();
		if (d->port)
			net_detach(d);
		dev_reset_state(d);
		plat_spin_lock(&d->evq_lock);
		for (int i = 0; i < MAX_VQS; i++)
			vq_reset(d, i);
		plat_spin_unlock(&d->evq_lock);
		wunlock();
		break;
	case VU_SET_MEM_TABLE:
		if (set_mem_table(d, &pl->mem, fds, nfds))
			ack = 1;
		nfds = 0;
		break;
	case VU_SET_VRING_NUM: {
		struct vq *q;

		/* ring positions are u16 counters taken modulo num */
		if (pl->state.index >= MAX_VQS || !pl->state.num ||
		    pl->state.num > 32768 ||
		    (pl->state.num & (pl->state.num - 1))) {
			ack = 1;
			break;
		}
		q = &d->vq[pl->state.index];
		wlock();
		q->num = pl->state.num;
		if (q->desc_ua && vq_map(d, q))
			ack = 1;
		wunlock();
		break;
	}
	case VU_SET_VRING_ADDR: {
		struct vq *q;

		if (pl->addr.index >= MAX_VQS) {
			ack = 1;
			break;
		}
		q = &d->vq[pl->addr.index];
		wlock();
		q->desc_ua = pl->addr.desc;
		q->avail_ua = pl->addr.avail;
		q->used_ua = pl->addr.used;
		/* with no size yet, SET_VRING_NUM maps it */
		if (vq_map(d, q) && q->num)
			ack = 1;
		wunlock();
		if (ack)
			LOG("dev%d: vq%u addresses not in guest memory", d->id,
			    pl->addr.index);
		break;
	}
	case VU_SET_VRING_BASE:
		if (pl->state.index >= MAX_VQS) {
			ack = 1;
			break;
		}
		wlock();
		d->vq[pl->state.index].last_avail = pl->state.num;
		d->vq[pl->state.index].used_idx = pl->state.num;
		d->vq[pl->state.index].signalled_valid = false;
		d->vq[pl->state.index].broken = false;
		wunlock();
		break;
	case VU_GET_VRING_BASE: {
		struct vu_state st = pl->state;

		if (st.index >= MAX_VQS)
			break;
		wlock();
		if (d->port)
			net_detach(d);
		else
			dev_stop(d, &d->vq[st.index]);
		plat_spin_lock(&d->evq_lock);
		vq_stop(d, st.index);
		plat_spin_unlock(&d->evq_lock);
		wunlock();
		st.num = d->vq[st.index].last_avail;
		rc = vu_reply(d, h.request, &st, sizeof(st));
		need_ack = false;
		break;
	}
	case VU_SET_VRING_KICK:
	case VU_SET_VRING_CALL:
	case VU_SET_VRING_ERR: {
		uint32_t idx = pl->u64 & 0xff;
		int fd = (pl->u64 & VU_VRING_NOFD) || nfds < 1 ? -1 : fds[0];

		if (fd >= 0)
			fds[0] = -1;
		if (idx >= MAX_VQS) {
			if (fd >= 0)
				close(fd);
			ack = 1;
			break;
		}
		struct vq *q = &d->vq[idx];

		if (h.request == VU_SET_VRING_ERR) {
			if (fd >= 0)
				close(fd);
		} else if (h.request == VU_SET_VRING_CALL) {
			/* Workers signal under their mutexes: once wlock has
			 * them all, none still holds the old fd. */
			wlock();
			plat_spin_lock(&d->evq_lock);
			int old = q->callfd;

			q->callfd = fd;
			plat_spin_unlock(&d->evq_lock);
			wunlock();
			if (old >= 0)
				close(old);
		} else {
			if (q->kickfd >= 0) {
				ep_del(q->kickfd);
				worker_unwatch_all(q->kickfd);
				close(q->kickfd);
			}
			q->kickfd = fd;
			if (fd >= 0) {
				/* where kicks are drained, the main thread
				 * reads doorbells too (worker_watch()) */
				bool doorbell = !PLAT_NOTIFIER_DRAIN &&
					(d->kind == KIND_NET ?
					 idx & 1 : idx == SHMRDMA_VQ_DOORBELL);

				if (doorbell && d->kind == KIND_NET) {
					/* its worker, if the queue is served */
					wlock();
					struct worker *w = d->port->q[idx / 2].w;

					if (w)
						worker_watch(w, fd);
					wunlock();
				} else if (doorbell) {
					/* workers already serving d watch it */
					for (int i = 0; i < g_nworkers; i++) {
						struct worker *w = &g_workers[i];
						bool mine = false;

						pthread_mutex_lock(&w->mtx);
						for (int j = 0; j < w->nqps; j++)
							mine |= w->qps[j]->dev == d;
						pthread_mutex_unlock(&w->mtx);
						if (mine)
							worker_watch(w, fd);
					}
				} else {
					ep_add(fd, &q->kick_tag, EP_KICK, d, idx);
				}
			}
			wlock();
			q->started = true;
			if (!(d->features & F_PROTOCOL_FEATURES))
				q->enabled = true;
			if (d->kind == KIND_NET) {
				if (d->vq[NET_RXQ(0)].started &&
				    d->vq[NET_TXQ(0)].started)
					net_attach(d);
				if (d->port->attached)
					netq_serve(&d->port->q[idx / 2]);
			} else if (d->stopped) {
				dev_restart(d, q);
			}
			wunlock();
			/* catch anything posted before we listened */
			if (d->kind == KIND_NET)
				wake_workers_for(d);
			else
				dev_resume(d);
		}
		break;
	}
	case VU_SET_VRING_ENABLE:
		if (pl->state.index >= MAX_VQS) {
			ack = 1;
			break;
		}
		wlock();
		d->vq[pl->state.index].enabled = pl->state.num;
		if (d->kind == KIND_NET) {
			struct netq *nq = &d->port->q[pl->state.index / 2];

			if (pl->state.num)
				netq_serve(nq);
			else if (pl->state.index & 1)
				netq_unserve(nq);
		}
		wunlock();
		if (d->kind == KIND_RDMA && pl->state.num)
			dev_resume(d);
		if (d->kind == KIND_NET && pl->state.num)
			wake_workers_for(d);
		break;
	case VU_SET_LOG_BASE:
	case VU_SET_LOG_FD:
		break;
	default:
		LOG("dev%d: unhandled vhost-user request %u", d->id, h.request);
		if (h.flags & VU_FLAG_NEED_REPLY)
			ack = 1;
		break;
	}
	if (need_ack && rc == 0)
		rc = vu_reply(d, h.request, &ack, 8);
out:
	for (int i = 0; i < nfds; i++)
		if (fds[i] >= 0)
			close(fds[i]);
	free(pl);
	return rc;
}

/* held so that a connection can still be taken and closed with no fds left */
static int g_spare_fd = -1;

/* each VM costs a few fds per queue: take what the hard limit allows */
static void raise_nofile(void)
{
	struct rlimit rl;

	if (getrlimit(RLIMIT_NOFILE, &rl) || rl.rlim_cur >= rl.rlim_max)
		return;
	rl.rlim_cur = rl.rlim_max;
	/* macOS refuses a soft limit above OPEN_MAX */
	if (setrlimit(RLIMIT_NOFILE, &rl) && rl.rlim_max > 10240) {
		rl.rlim_cur = 10240;
		setrlimit(RLIMIT_NOFILE, &rl);
	}
}

static void accept_conn(int lsock, int kind)
{
	int s = plat_accept(lsock);
	struct vdev *d;

	if (s < 0) {
		/* else the listener stays readable and the main loop spins */
		if ((errno == EMFILE || errno == ENFILE) && g_spare_fd >= 0) {
			close(g_spare_fd);
			s = plat_accept(lsock);
			if (s >= 0)
				close(s);
			g_spare_fd = open("/dev/null", O_RDONLY | O_CLOEXEC);
			LOG("connection refused: out of file descriptors");
		}
		return;
	}
	d = calloc(1, sizeof(*d));
	if (!d) {
		LOG("connection refused: out of memory");
		close(s);
		return;
	}
	d->id = g_next_dev_id++;
	d->sock = s;
	d->kind = kind;
	for (int i = 0; i < MAX_VQS; i++) {
		d->vq[i].kickfd = -1;
		d->vq[i].callfd = -1;
	}
	plat_spin_init(&d->evq_lock);
	plat_spin_init(&d->mr_lock);
	plat_spin_init(&d->log_lock);
	d->log_t = now_ns();
	d->log_tokens = GLOG_BURST;
	wlock();
	d->next = g_devs;
	g_devs = d;
	wunlock();
	LOG("dev%d: %s connected", d->id, kind == KIND_NET ? "net" : "rdma");
	if ((kind == KIND_NET && net_port_new(d)) ||
	    ep_add(s, &d->sock_tag, EP_SOCK, d, 0))
		dev_destroy(d);
}

static void on_kick(struct vdev *d, int idx)
{
	struct vq *q = &d->vq[idx];

	if (q->kickfd < 0)
		return;
	if (plat_notifier_drain(q->kickfd)) {
		/* QEMU closed its end without telling us; no more kicks */
		LOG("dev%d: vq%d kick fd closed by QEMU", d->id, idx);
		ep_del(q->kickfd);
		return;
	}
	if (d->kind == KIND_NET) {
		/* TX: frames to send; RX: buffers for a stalled sender */
		if (idx & 1)
			wake_workers_for(d);
		else
			wake_workers();
		return;
	}
	switch (idx) {
	case SHMRDMA_VQ_CTRL:
		ctrl_process(d);
		break;
	case SHMRDMA_VQ_EVENT:
		plat_spin_lock(&d->evq_lock);
		if (d->irq_pending)
			dev_send_irq(d);
		plat_spin_unlock(&d->evq_lock);
		break;
	case SHMRDMA_VQ_DOORBELL:
		wake_workers_for(d);
		break;
	}
}

static volatile sig_atomic_t g_stats;

static void on_usr1(int sig)
{
	(void)sig;
	g_stats = 1;
}

static void dump_stats(void)
{
	char gs[64];

	for (struct netport *p = g_ports; p; p = p->next) {
		uint8_t m[6];

		mac_bytes(RLX(&p->mac), m);
		LOG("dev%d: net %02x:%02x:%02x:%02x:%02x:%02x tx %" PRIu64
		    "/%" PRIu64 " rx %" PRIu64 "/%" PRIu64 " drops %" PRIu64,
		    p->dev->id, m[0], m[1], m[2], m[3], m[4], m[5],
		    p->tx_pkts, p->tx_bytes, p->rx_pkts, p->rx_bytes, p->drops);
	}
	for (struct vdev *d = g_devs; d; d = d->next) {
		LOG("dev%d: init %d", d->id, d->initialized);
		for (int i = 0; i < SHMRDMA_MAX_GID; i++)
			if (d->gid_valid[i]) {
				gid_str(d->gids[i], gs, sizeof(gs));
				LOG("  gid[%d] %s", i, gs);
			}
		for (int i = 0; i < SHMRDMA_MAX_QP; i++) {
			struct qp *qp = d->qps[i];

			if (!qp)
				continue;
			gid_str(qp->dgid, gs, sizeof(gs));
			LOG("  qp%u type %u state %u -> %s/%u sq %u/%u rq %u/%u "
			    "tx %" PRIu64 " bytes %" PRIu64 " wrs", qp->qpn,
			    qp->type, qp->state, gs, qp->dqpn, qp->sq_cons,
			    RLX(&qp->rec->sq_prod), qp->rq_cons,
			    RLX(&qp->rec->rq_prod), qp->tx_bytes, qp->tx_wr);
		}
	}
}

static void usage(void)
{
	fprintf(stderr,
		"usage: shmrdmad [-s rdma_socket] [-n net_socket] [-w workers] "
		"[-c copiers] [-p spin_us] [-v]\n");
	exit(2);
}

int main(int argc, char **argv)
{
	int opt;

	while ((opt = getopt(argc, argv, "s:n:w:p:c:v")) != -1) {
		switch (opt) {
		case 'n': g_net_sock_path = optarg; break;
		case 'c': g_ncopiers = atoi(optarg); break;
		case 's': g_sock_path = optarg; break;
		case 'w': g_nworkers = atoi(optarg); break;
		case 'p': g_spin_ns = strtoull(optarg, NULL, 0) * 1000; break;
		case 'v': g_verbose++; break;
		default: usage();
		}
	}
	if (g_nworkers < 1)
		g_nworkers = 1;
	if (g_nworkers > 256)
		g_nworkers = 256;
	if (g_ncopiers < 0)
		g_ncopiers = 0;
	if (g_ncopiers > 256)
		g_ncopiers = 256;
	{
		struct sockaddr_un sa;

		if (strlen(g_sock_path) >= sizeof(sa.sun_path) ||
		    strlen(g_net_sock_path) >= sizeof(sa.sun_path)) {
			LOG("socket paths must be shorter than %zu bytes",
			    sizeof(sa.sun_path));
			return 2;
		}
	}
	raise_nofile();
	g_spare_fd = open("/dev/null", O_RDONLY | O_CLOEXEC);
	plat_init();
	plat_spin_init(&g_mac_lock);

	signal(SIGPIPE, SIG_IGN);
	signal(SIGUSR1, on_usr1);
	setvbuf(stderr, NULL, _IOLBF, 0);

	g_workers = calloc(g_nworkers, sizeof(*g_workers));
	if (!g_workers) {
		LOG("out of memory");
		return 1;
	}
	for (int i = 0; i < g_nworkers; i++) {
		struct worker *w = &g_workers[i];

		w->idx = i;
		pthread_mutex_init(&w->mtx, NULL);
		w->evfd = plat_notifier_new(&w->evfd_w);
		w->epfd = plat_poller_new();
		plat_poller_add(w->epfd, w->evfd, (void *)(intptr_t)w->evfd,
				false);
	}
	for (int i = 0; i < g_nworkers; i++)
		pthread_create(&g_workers[i].thr, NULL, worker_main,
			       &g_workers[i]);
	for (int i = 0; i < g_ncopiers; i++) {
		pthread_t t;

		pthread_create(&t, NULL, copier_main, NULL);
	}

	struct sockaddr_un sa = { .sun_family = AF_UNIX };
	static struct ep_tag listen_tag, listen_net_tag;

	/* The sockets hand out guest RAM: only this user (and root) may
	 * connect, from the moment they exist.
	 */
	umask(077);
	memcpy(sa.sun_path, g_sock_path, strlen(g_sock_path));
	unlink(g_sock_path);
	g_lsock = plat_unix_socket();
	if (bind(g_lsock, (struct sockaddr *)&sa, sizeof(sa)) ||
	    listen(g_lsock, 64)) {
		LOG("listen %s: %s", g_sock_path, strerror(errno));
		return 1;
	}
	chmod(g_sock_path, 0600);
	g_epfd = plat_poller_new();
	ep_add(g_lsock, &listen_tag, EP_LISTEN, NULL, 0);
	if (*g_net_sock_path) {
		struct sockaddr_un na = { .sun_family = AF_UNIX };

		memcpy(na.sun_path, g_net_sock_path, strlen(g_net_sock_path));
		unlink(g_net_sock_path);
		g_lsock_net = plat_unix_socket();
		if (bind(g_lsock_net, (struct sockaddr *)&na, sizeof(na)) ||
		    listen(g_lsock_net, 64)) {
			LOG("listen %s: %s", g_net_sock_path, strerror(errno));
			return 1;
		}
		chmod(g_net_sock_path, 0600);
		ep_add(g_lsock_net, &listen_net_tag, EP_LISTEN_NET, NULL, 0);
		LOG("net switch on %s", g_net_sock_path);
	}
	LOG("listening on %s, %d workers, %d copiers, spin %" PRIu64 " us",
	    g_sock_path, g_nworkers, g_ncopiers, g_spin_ns / 1000);

	uint64_t next_stats = 0;

	for (;;) {
		struct plat_event evs[32];
		uint64_t t = now_ns();
		int n;

		if (t >= next_stats) {
			net_write_stats();
			next_stats = t + 1000000000ull;
		}
		n = plat_poller_wait(g_epfd, evs, 32,
				     (next_stats - t) / 1000000 + 1);
		if (g_stats) {
			g_stats = 0;
			dump_stats();
		}
		for (int i = 0; i < n; i++) {
			struct ep_tag *t = evs[i].tag;

			if (!t)
				continue;
			switch (t->kind) {
			case EP_LISTEN:
				accept_conn(g_lsock, KIND_RDMA);
				break;
			case EP_LISTEN_NET:
				accept_conn(g_lsock_net, KIND_NET);
				break;
			case EP_SOCK:
				if (evs[i].hup ||
				    handle_msg(t->d)) {
					struct vdev *d = t->d;

					/* d's tags go with it: drop its events */
					for (int j = i + 1; j < n; j++) {
						struct ep_tag *u = evs[j].tag;

						if (u && u->d == d)
							evs[j].tag = NULL;
					}
					dev_destroy(d);
				}
				break;
			case EP_KICK:
				on_kick(t->d, t->vq);
				break;
			}
		}
	}
}
