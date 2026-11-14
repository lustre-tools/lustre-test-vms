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
#include <sys/epoll.h>
#include <sys/eventfd.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#include <immintrin.h>

#include "../include/shmrdma_abi.h"

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
#define REL(p, v)	__atomic_store_n((p), (v), __ATOMIC_RELEASE)
#define FENCE()		__atomic_thread_fence(__ATOMIC_SEQ_CST)

static inline uint64_t now_ns(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (uint64_t)ts.tv_sec * 1000000000ull + ts.tv_nsec;
}

static inline void cpu_relax(void)
{
#if defined(__x86_64__)
	__builtin_ia32_pause();
#endif
}

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
#define PF_MQ			(1ull << 0)
#define PF_REPLY_ACK		(1ull << 3)

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
#define VRING_AVAIL_F_NO_INTERRUPT 1

struct vring_avail {
	uint16_t flags;
	uint16_t idx;
	uint16_t ring[];
};

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

struct mapping {
	uint64_t gpa, size, uaddr;
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

struct vdev;

struct cq {
	struct vdev *dev;
	uint32_t cqn;
	struct shmrdma_cq_rec *rec;
	struct ring ring;
	uint32_t prod;
	pthread_spinlock_t lock;
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
	bool valid;
	uint64_t pages[SHMRDMA_MAX_FRPL];
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
	struct qp *peer;
	uint64_t peer_gen;
	pthread_spinlock_t rq_lock;
	struct worker *w;
	uint64_t stall_since;
	bool fatal_sent;
	/* stats */
	uint64_t tx_bytes, tx_wr;
};

struct netport;
static struct vdev *port_dev(struct netport *p);

enum { KIND_RDMA, KIND_NET };

struct vdev {
	int id;
	int sock;
	int kind;
	struct netport *port;
	bool dead;
	uint64_t features;
	uint64_t pfeatures;
	int nmap;
	struct mapping map[VU_MAX_REGIONS];
	/* old mappings kept until teardown: cached pointers may use them */
	int nold;
	struct mapping old[64];
	struct vq vq[SHMRDMA_NUM_VQS];
	pthread_spinlock_t evq_lock;	/* event vq + EQ */

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

	struct vdev *next;
};

struct worker {
	int idx;
	pthread_t thr;
	pthread_mutex_t mtx;
	int evfd;
	int epfd;		/* evfd and the doorbells of its devices */
	int sleeping;
	struct qp **qps;
	int nqps, cap;
	struct netport **ports;
	int nports, pcap;
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
static int g_lsock;

/* writer lock: hold every worker's mutex */
static void wlock(void)
{
	for (int i = 0; i < g_nworkers; i++)
		pthread_mutex_lock(&g_workers[i].mtx);
}

static void wunlock(void)
{
	for (int i = g_nworkers - 1; i >= 0; i--)
		pthread_mutex_unlock(&g_workers[i].mtx);
}

/* wake sleeping workers; only those with a QP on d, unless d is NULL */
static void wake_workers_for(struct vdev *d)
{
	uint64_t one = 1;

	for (int i = 0; i < g_nworkers; i++) {
		struct worker *w = &g_workers[i];
		bool mine = !d;

		if (!__atomic_load_n(&w->sleeping, __ATOMIC_SEQ_CST))
			continue;
		if (!mine) {
			pthread_mutex_lock(&w->mtx);
			for (int j = 0; j < w->nqps && !mine; j++)
				mine = w->qps[j]->dev == d;
			for (int j = 0; j < w->nports && !mine; j++)
				mine = port_dev(w->ports[j]) == d;
			pthread_mutex_unlock(&w->mtx);
		}
		if (mine)
			(void)!write(w->evfd, &one, 8);
	}
}

static void wake_workers(void)
{
	wake_workers_for(NULL);
}

/*
 * The doorbell of a device a worker serves goes straight into the
 * worker's epoll, edge-triggered: every guest kick wakes it with no
 * hop through the main thread, and nobody needs to read the eventfd.
 */
static void worker_watch(struct worker *w, int fd)
{
	struct epoll_event ev = { .events = EPOLLIN | EPOLLET };

	if (fd < 0)
		return;
	ev.data.fd = fd;
	if (epoll_ctl(w->epfd, EPOLL_CTL_ADD, fd, &ev) && errno != EEXIST)
		LOG("worker%d: watch fd %d: %s", w->idx, fd, strerror(errno));
}

/* ---------------------------------------------------------------- */
/* guest memory                                                     */

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

static void vq_signal(struct vq *q)
{
	uint64_t one = 1;

	if (q->callfd < 0)
		return;
	if (ACQ(&q->avail->flags) & VRING_AVAIL_F_NO_INTERRUPT)
		return;
	(void)!write(q->callfd, &one, 8);
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

	if (!vq_ready(q)) {
		d->irq_pending = true;
		return;
	}
	if (q->last_avail == ACQ(&q->avail->idx)) {
		d->irq_pending = true;
		return;
	}
	uint16_t head = q->avail->ring[q->last_avail % q->num];

	q->last_avail++;
	d->irq_pending = false;
	vq_push_used(q, head, 0);
	FENCE();
	vq_signal(q);
}

static void dev_raise_eq(struct vdev *d, uint32_t cqn, uint32_t type)
{
	if (!d->eq)
		return;
	pthread_spin_lock(&d->evq_lock);
	struct shmrdma_eqe *e = &d->eq[d->eq_prod & (d->eq_entries - 1)];

	e->cqn = cqn;
	e->type = type;
	d->eq_prod++;
	REL(&d->shared->eq_prod, d->eq_prod);
	FENCE();
	if (RLX(&d->shared->eq_armed)) {
		REL(&d->shared->eq_armed, 0);
		dev_send_irq(d);
	}
	pthread_spin_unlock(&d->evq_lock);
}

/* ---------------------------------------------------------------- */
/* completion queues                                                */

static void cq_post(struct cq *cq, const struct shmrdma_cqe *cqe, bool solicited)
{
	bool event = false;

	pthread_spin_lock(&cq->lock);
	uint32_t cons = ACQ(&cq->rec->cons);

	if (cq->prod - cons >= cq->ring.entries) {
		if (!cq->overflowed)
			LOG("dev%d cq%u overflow (prod %u cons %u)",
			    cq->dev->id, cq->cqn, cq->prod, cons);
		cq->overflowed = true;
		pthread_spin_unlock(&cq->lock);
		return;
	}
	memcpy(ring_entry(&cq->ring, cq->prod), cqe, sizeof(*cqe));
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
	pthread_spin_unlock(&cq->lock);
	if (event)
		dev_raise_eq(cq->dev, cq->cqn, SHMRDMA_EQE_COMP);
}

/* ---------------------------------------------------------------- */
/* copying                                                          */

/*
 * Bulk copies use non-temporal stores: the destination is another
 * guest's buffer that will not be in this CPU's cache, and streaming
 * stores skip reading each destination line first.
 */
__attribute__((target("avx2")))
static void copy_stream(uint8_t *dst, const uint8_t *src, uint64_t n)
{
	uint64_t head = (32 - ((uintptr_t)dst & 31)) & 31;

	if (head > n)
		head = n;
	memcpy(dst, src, head);
	dst += head;
	src += head;
	n -= head;
	while (n >= 128) {
		__m256i a = _mm256_loadu_si256((const __m256i *)src);
		__m256i b = _mm256_loadu_si256((const __m256i *)(src + 32));
		__m256i c = _mm256_loadu_si256((const __m256i *)(src + 64));
		__m256i d = _mm256_loadu_si256((const __m256i *)(src + 96));

		_mm256_stream_si256((__m256i *)dst, a);
		_mm256_stream_si256((__m256i *)(dst + 32), b);
		_mm256_stream_si256((__m256i *)(dst + 64), c);
		_mm256_stream_si256((__m256i *)(dst + 96), d);
		dst += 128;
		src += 128;
		n -= 128;
	}
	memcpy(dst, src, n);
}

static bool g_have_avx2;

static inline void copy_bytes(uint8_t *dst, const uint8_t *src, uint64_t n,
			      bool nt)
{
	if (nt && g_have_avx2 && n >= 512)
		copy_stream(dst, src, n);
	else
		memcpy(dst, src, n);
}

/* ---------------------------------------------------------------- */
/* scatter/gather iteration                                         */

struct sg_iter {
	struct vdev *dev;
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
 * 0 at the end, or -1 on error (it->err set).
 */
static int64_t sg_next(struct sg_iter *it, uint64_t max, uint8_t **out)
{
	while (it->idx < it->nsge && it->off >= it->sge[it->idx].length) {
		it->idx++;
		it->off = 0;
	}
	if (it->idx >= it->nsge)
		return 0;

	const struct shmrdma_sge *s = &it->sge[it->idx];
	uint64_t left = s->length - it->off;
	uint64_t addr = s->addr + it->off;
	uint64_t gpa, avail;
	uint8_t *p;

	if (left > max)
		left = max;

	if (s->lkey == SHMRDMA_LOCAL_DMA_LKEY && !it->remote) {
		gpa = addr;
		p = gpa_hva(it->dev, gpa, &avail);
		if (!p) {
			it->err = WC_LOC_PROT_ERR;
			return -1;
		}
	} else {
		struct mr *mr = dev_mr(it->dev, s->lkey);

		if (!mr || (mr->access & it->need_access) != it->need_access ||
		    addr < mr->iova || addr - mr->iova >= mr->length ||
		    mr->length - (addr - mr->iova) < left) {
			it->err = it->remote ? WC_REM_ACCESS_ERR :
					       WC_LOC_PROT_ERR;
			return -1;
		}
		uint64_t moff = (addr - mr->iova) + mr->fpo;
		uint64_t pidx = moff >> mr->page_shift;
		uint64_t poff = moff & ((1ull << mr->page_shift) - 1);

		if (pidx >= mr->npages) {
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
		uint64_t ravail;

		p = gpa_hva(it->dev, gpa, &ravail);
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
			if (sl <= 0)
				return -1;
		}
		if (!dl) {
			dl = sg_next(dst, len, &dp);
			if (dl <= 0)
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
		_mm_sfence();
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

		if (dl <= 0)
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
struct copy_job {
	struct sg_iter dst, src;	/* positioned at the start */
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

static void part_range(struct copy_job *j, uint32_t i, uint64_t *off,
		       uint64_t *len)
{
	uint64_t per = (j->len / j->nparts) & ~4095ull;
	uint64_t start = per * i;

	*off = start;
	*len = (i == j->nparts - 1) ? j->len - start : per;
}

static void run_part(struct copy_job *j, uint32_t i)
{
	struct sg_iter d = j->dst, s = j->src;
	uint64_t off, len;

	part_range(j, i, &off, &len);
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
		while (!ACQ(&g_cp_queue) && now_ns() - t0 < 50000)
			cpu_relax();
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

/* copy, splitting across the helpers when it is large */
static int sg_copy_par(struct sg_iter *dst, struct sg_iter *src, uint64_t len)
{
	if (!g_ncopiers || len < g_split_min)
		return sg_copy_nt(dst, src, len, len >= g_nt_min);

	struct copy_job j = { .dst = *dst, .src = *src, .len = len };
	uint32_t n = len / g_part_min;
	uint32_t part;

	if (n > (uint32_t)g_ncopiers + 1)
		n = g_ncopiers + 1;
	if (n < 2)
		return sg_copy_nt(dst, src, len, len >= g_nt_min);
	j.nparts = n;
	pthread_mutex_lock(&g_cp_lock);
	j.qnext = g_cp_queue;
	g_cp_queue = &j;
	j.next = 1;		/* part 0 is ours */
	if (g_cp_sleepers)
		pthread_cond_broadcast(&g_cp_cond);
	pthread_mutex_unlock(&g_cp_lock);
	run_part(&j, 0);
	/* help with our own job, then wait for the rest */
	for (;;) {
		pthread_mutex_lock(&g_cp_lock);
		if (j.next < j.nparts) {
			part = j.next++;
			if (j.next == j.nparts) {
				for (struct copy_job **pp = &g_cp_queue; *pp;
				     pp = &(*pp)->qnext)
					if (*pp == &j) {
						*pp = j.qnext;
						break;
					}
			}
			pthread_mutex_unlock(&g_cp_lock);
			run_part(&j, part);
			continue;
		}
		pthread_mutex_unlock(&g_cp_lock);
		break;
	}
	while (ACQ(&j.done) < j.nparts)
		cpu_relax();
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

	if (!p)
		return NULL;
	qp->peer = p;
	return p;
}

static void qp_to_err(struct qp *qp)
{
	if (qp->state != QPS_ERR) {
		qp->state = QPS_ERR;
		if (!qp->fatal_sent) {
			qp->fatal_sent = true;
			dev_raise_eq(qp->dev, qp->qpn, SHMRDMA_EQE_QP_FATAL);
		}
	}
}

static void qp_flush_rq(struct qp *qp)
{
	struct shmrdma_cqe c = {0};

	pthread_spin_lock(&qp->rq_lock);
	uint32_t prod = ACQ(&qp->rec->rq_prod);

	while (qp->rq_cons != prod) {
		struct shmrdma_rq_wqe *w = ring_entry(&qp->rq, qp->rq_cons);

		c.wr_id = w->wr_id;
		c.qpn = qp->qpn;
		c.opcode = SHMRDMA_WC_RECV;
		c.status = WC_WR_FLUSH_ERR;
		cq_post(qp->rcq, &c, false);
		qp->rq_cons++;
	}
	REL(&qp->rec->rq_cons, qp->rq_cons);
	pthread_spin_unlock(&qp->rq_lock);
}

enum { EX_DONE, EX_STALL };

static bool peer_ok(struct qp *qp, struct qp *peer)
{
	return peer && !peer->dev->dead &&
	       (peer->state == QPS_RTR || peer->state == QPS_RTS) &&
	       peer->type == SHMRDMA_QPT_RC && peer->dqpn == qp->qpn;
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

	if (!pd || w->ud.dqpn >= SHMRDMA_MAX_QP)
		return WC_SUCCESS;	/* dropped, as on a wire */
	peer = pd->qps[w->ud.dqpn];
	if (!peer || peer->type == SHMRDMA_QPT_RC ||
	    (peer->state != QPS_RTR && peer->state != QPS_RTS))
		return WC_SUCCESS;
	if (peer->type != SHMRDMA_QPT_GSI && peer->qkey != w->ud.qkey)
		return WC_SUCCESS;

	uint64_t len = (w->flags & SHMRDMA_WQE_INLINE) ? w->send.inline_len :
			sg_total(sge, w->num_sge);
	struct shmrdma_cqe c = {0};
	int rc = WC_SUCCESS;

	pthread_spin_lock(&peer->rq_lock);
	uint32_t prod = ACQ(&peer->rec->rq_prod);

	if (peer->rq_cons == prod) {
		pthread_spin_unlock(&peer->rq_lock);
		DBG("dev%d qp%u: UD drop, no recv on dev%d qp%u", qp->dev->id,
		    qp->qpn, pd->id, peer->qpn);
		return WC_SUCCESS;
	}
	struct shmrdma_rq_wqe *rw = ring_entry(&peer->rq, peer->rq_cons);
	struct shmrdma_sge rsge[SHMRDMA_MAX_SGE];
	uint32_t rn = rw->num_sge > SHMRDMA_MAX_SGE ? SHMRDMA_MAX_SGE :
		      rw->num_sge;

	memcpy(rsge, rw->sge, rn * sizeof(rsge[0]));
	c.wr_id = rw->wr_id;
	c.qpn = peer->qpn;
	c.opcode = SHMRDMA_WC_RECV;
	c.src_qp = qp->qpn;
	c.flags = SHMRDMA_CQE_GRH;

	struct sg_iter dst = { .dev = pd, .sge = rsge, .nsge = rn,
			       .need_access = 0 };
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
		struct sg_iter src = { .dev = qp->dev, .sge = sge,
				       .nsge = w->num_sge };

		if (sg_copy(&dst, &src, len)) {
			if (src.err) {
				rc = src.err;
				pthread_spin_unlock(&peer->rq_lock);
				return rc;
			}
			c.status = dst.err;
		}
	}
	c.byte_len = len + SHMRDMA_GRH_BYTES;
	peer->rq_cons++;
	REL(&peer->rec->rq_cons, peer->rq_cons);
	pthread_spin_unlock(&peer->rq_lock);
	cq_post(peer->rcq, &c, w->flags & SHMRDMA_WQE_SOLICITED);
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

	pthread_spin_lock(&peer->rq_lock);
	uint32_t prod = ACQ(&peer->rec->rq_prod);

	if (peer->rq_cons == prod) {
		pthread_spin_unlock(&peer->rq_lock);
		return -1;
	}
	struct shmrdma_rq_wqe *rw = ring_entry(&peer->rq, peer->rq_cons);
	struct shmrdma_sge rsge[SHMRDMA_MAX_SGE];
	uint32_t rn = rw->num_sge > SHMRDMA_MAX_SGE ? SHMRDMA_MAX_SGE :
		      rw->num_sge;

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
			struct sg_iter dst = { .dev = peer->dev, .sge = rsge,
					       .nsge = rn };

			if (w->flags & SHMRDMA_WQE_INLINE) {
				if (sg_copy_in(&dst, (const uint8_t *)w->sge,
					       len)) {
					c.status = dst.err;
					rc = WC_REM_OP_ERR;
				}
			} else {
				struct sg_iter src = { .dev = qp->dev,
						       .sge = sge,
						       .nsge = w->num_sge };

				if (sg_copy_par(&dst, &src, len)) {
					if (src.err) {
						/* local fault: peer unaffected */
						pthread_spin_unlock(&peer->rq_lock);
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
		struct mr *mr = dev_mr(peer->dev, w->ex);

		if (mr)
			REL(&mr->valid, false);
	}
	peer->rq_cons++;
	REL(&peer->rec->rq_cons, peer->rq_cons);
	pthread_spin_unlock(&peer->rq_lock);
	cq_post(peer->rcq, &c, w->flags & SHMRDMA_WQE_SOLICITED);
	if (c.status != WC_SUCCESS) {
		peer->state = QPS_ERR;
		dev_raise_eq(peer->dev, peer->qpn, SHMRDMA_EQE_QP_FATAL);
	}
	return rc;
}

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
	mr = d->mrs[idx];
	if (!mr) {
		mr = calloc(1, sizeof(*mr));
		if (!mr)
			return WC_GENERAL_ERR;
		d->mrs[idx] = mr;
	}
	REL(&mr->valid, false);
	mr->key = w->reg.key;
	mr->iova = w->reg.iova;
	mr->length = w->reg.length;
	mr->page_shift = w->reg.page_shift;
	mr->npages = w->reg.npages;
	mr->access = w->reg.access;
	mr->fpo = w->ex & ((1u << w->reg.page_shift) - 1);
	memcpy(mr->pages, pl, w->reg.npages * 8ull);
	REL(&mr->valid, true);
	return WC_SUCCESS;
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
		if (qp->type != SHMRDMA_QPT_RC)
			return exec_ud_send(qp, w, sge);
		peer = qp_peer(qp);
		if (!peer_ok(qp, peer))
			return WC_RETRY_EXC_ERR;
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
		peer = qp_peer(qp);
		if (!peer_ok(qp, peer))
			return WC_RETRY_EXC_ERR;
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
		struct sg_iter dst = { .dev = peer->dev, .sge = &rs, .nsge = 1,
				       .remote = true,
				       .need_access = SHMRDMA_ACC_REMOTE_WRITE };

		if (len) {
			if (w->flags & SHMRDMA_WQE_INLINE) {
				if (sg_copy_in(&dst, (const uint8_t *)w->sge, len))
					return dst.err;
			} else {
				struct sg_iter src = { .dev = qp->dev, .sge = sge,
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
		peer = qp_peer(qp);
		if (!peer_ok(qp, peer))
			return WC_RETRY_EXC_ERR;
		uint64_t len = sg_total(sge, w->num_sge);
		struct shmrdma_sge rs = { .addr = w->rdma.remote_addr,
					  .length = len, .lkey = w->rdma.rkey };
		struct sg_iter src = { .dev = peer->dev, .sge = &rs, .nsge = 1,
				       .remote = true,
				       .need_access = SHMRDMA_ACC_REMOTE_READ };
		struct sg_iter dst = { .dev = qp->dev, .sge = sge,
				       .nsge = w->num_sge };

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
		mr = qp->dev->mrs[idx];
		if (mr && mr->key == w->ex)
			REL(&mr->valid, false);
		return WC_SUCCESS;
	}
	}
	*opc = SHMRDMA_WC_SEND;
	return WC_LOC_QP_OP_ERR;
}

/* process up to budget send WQEs; returns work done */
static int qp_process(struct qp *qp, int budget, bool *stalled)
{
	int done = 0;

	if (qp->dev->dead)
		return 0;

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
		if (st != WC_SUCCESS || (w->flags & SHMRDMA_WQE_SIGNALED)) {
			c.wr_id = w->wr_id;
			c.qpn = qp->qpn;
			c.opcode = opc;
			c.status = st;
			cq_post(qp->scq, &c, false);
		}
		if (st != WC_SUCCESS && st != WC_WR_FLUSH_ERR) {
			LOG("dev%d qp%u: wqe op %u failed status %d",
			    qp->dev->id, qp->qpn, w->opcode, st);
			qp_to_err(qp);
		}
		qp->tx_wr++;
		qp->sq_cons++;
		REL(&qp->rec->sq_cons, qp->sq_cons);
		done++;
	}
	return done;
}

static bool qp_has_work(struct qp *qp)
{
	if (qp->dev->dead)
		return false;
	if (qp->state == QPS_ERR && ACQ(&qp->rec->rq_prod) != qp->rq_cons)
		return true;
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

		pthread_mutex_lock(&w->mtx);
		for (int i = 0; i < w->nqps; i++)
			did += qp_process(w->qps[i], 32, &stalled);
		for (int i = 0; i < w->nports; i++)
			did += net_tx_process(w->ports[i], 64, &stalled);
		pthread_mutex_unlock(&w->mtx);

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
			REL(&w->qps[i]->rec->need_kick, 1);
		for (int i = 0; i < w->nports; i++)
			net_set_notify(w->ports[i], true);
		FENCE();
		for (int i = 0; i < w->nqps && !work; i++)
			work = qp_has_work(w->qps[i]);
		for (int i = 0; i < w->nports && !work; i++)
			work = net_has_work(w->ports[i]);
		pthread_mutex_unlock(&w->mtx);

		if (!work) {
			struct epoll_event evs[16];
			uint64_t v;
			int ne = epoll_wait(w->epfd, evs, 16, stalled ? 1 : 1000);

			for (int i = 0; i < ne; i++)
				if (evs[i].data.fd == w->evfd)
					(void)!read(w->evfd, &v, 8);
		}
		__atomic_store_n(&w->sleeping, 0, __ATOMIC_SEQ_CST);
		pthread_mutex_lock(&w->mtx);
		for (int i = 0; i < w->nqps; i++)
			REL(&w->qps[i]->rec->need_kick, 0);
		for (int i = 0; i < w->nports; i++)
			net_set_notify(w->ports[i], false);
		pthread_mutex_unlock(&w->mtx);
		idle_since = now_ns();
	}
	return NULL;
}

/* caller holds wlock */
static void worker_add_qp(struct qp *qp)
{
	struct worker *best = &g_workers[0];

	for (int i = 1; i < g_nworkers; i++)
		if (g_workers[i].nqps + g_workers[i].nports <
		    best->nqps + best->nports)
			best = &g_workers[i];
	if (best->nqps == best->cap) {
		best->cap = best->cap ? best->cap * 2 : 16;
		best->qps = realloc(best->qps, best->cap * sizeof(*best->qps));
	}
	best->qps[best->nqps++] = qp;
	worker_watch(best, qp->dev->vq[SHMRDMA_VQ_DOORBELL].kickfd);
	qp->w = best;
	REL(&qp->rec->need_kick, ACQ(&best->sleeping) ? 1 : 0);
}

static void worker_del_qp(struct qp *qp)
{
	struct worker *w = qp->w;

	for (int i = 0; i < w->nqps; i++) {
		if (w->qps[i] == qp) {
			w->qps[i] = w->qps[--w->nqps];
			break;
		}
	}
	qp->w = NULL;
}

/* ---------------------------------------------------------------- */
/* control commands                                                 */

static int ring_setup(struct vdev *d, struct ring *r,
		      const struct shmrdma_ring_desc *rd)
{
	if (!rd->entries || (rd->entries & (rd->entries - 1)) ||
	    !rd->entry_size || (rd->entry_size & (rd->entry_size - 1)) ||
	    rd->entry_size > 4096 ||
	    (uint64_t)rd->entries * rd->entry_size > (uint64_t)rd->npages * 4096)
		return -EINVAL;
	uint64_t *pl = gpa_range(d, rd->pages_gpa, rd->npages * 8ull);

	if (!pl)
		return -EFAULT;
	r->pages = calloc(rd->npages, sizeof(*r->pages));
	if (!r->pages)
		return -ENOMEM;
	for (uint32_t i = 0; i < rd->npages; i++) {
		r->pages[i] = gpa_range(d, pl[i], 4096);
		if (!r->pages[i] || (pl[i] & 4095)) {
			free(r->pages);
			r->pages = NULL;
			return -EFAULT;
		}
	}
	r->entries = rd->entries;
	r->esz = rd->entry_size;
	r->npages = rd->npages;
	return 0;
}

static void qp_free(struct qp *qp)
{
	free(qp->sq.pages);
	free(qp->rq.pages);
	pthread_spin_destroy(&qp->rq_lock);
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
		free(cq->ring.pages);
		pthread_spin_destroy(&cq->lock);
		free(cq);
		d->cqs[i] = NULL;
	}
	if (d->mrs) {
		for (int i = 0; i < SHMRDMA_MAX_MR; i++) {
			free(d->mrs[i]);
			d->mrs[i] = NULL;
		}
	}
	memset(d->gid_valid, 0, sizeof(d->gid_valid));
	d->initialized = false;
	d->shared = NULL;
	d->eq = NULL;
	d->eq_prod = 0;
	d->irq_pending = false;
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

static int do_cmd(struct vdev *d, const struct shmrdma_cmd *c,
		  struct shmrdma_resp *r)
{
	char gs[64];

	switch (c->cmd) {
	case SHMRDMA_CMD_INIT: {
		if (c->init.abi != SHMRDMA_ABI_VERSION)
			return -EPROTO;
		uint32_t n = c->init.eq_entries;

		if (!n || (n & (n - 1)))
			return -EINVAL;
		void *sh = gpa_range(d, c->init.shared_gpa, sizeof(*d->shared));
		void *eq = gpa_range(d, c->init.eq_gpa, n * sizeof(struct shmrdma_eqe));

		if (!sh || !eq)
			return -EFAULT;
		wlock();
		dev_reset_state(d);
		d->shared = sh;
		d->eq = eq;
		d->eq_entries = n;
		d->eq_prod = RLX(&d->shared->eq_cons);
		if (!d->mrs)
			d->mrs = calloc(SHMRDMA_MAX_MR, sizeof(*d->mrs));
		d->initialized = true;
		wunlock();
		r->val = d->id;
		LOG("dev%d: initialized, eq %u entries", d->id, n);
		return 0;
	}
	case SHMRDMA_CMD_ADD_GID:
		if (c->gid.index >= SHMRDMA_MAX_GID)
			return -EINVAL;
		wlock();
		memcpy(d->gids[c->gid.index], c->gid.gid, 16);
		d->gid_valid[c->gid.index] = true;
		__atomic_add_fetch(&g_gen, 1, __ATOMIC_SEQ_CST);
		wunlock();
		gid_str(c->gid.gid, gs, sizeof(gs));
		LOG("dev%d: gid[%u] = %s", d->id, c->gid.index, gs);
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
		cq->rec = gpa_range(d, c->create_cq.rec_gpa, sizeof(*cq->rec));
		if (!cq->rec) {
			free(cq);
			return -EFAULT;
		}
		rc = ring_setup(d, &cq->ring, &c->create_cq.ring);
		if (rc) {
			free(cq);
			return rc;
		}
		if (cq->ring.esz < sizeof(struct shmrdma_cqe)) {
			free(cq->ring.pages);
			free(cq);
			return -EINVAL;
		}
		pthread_spin_init(&cq->lock, 0);
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
		free(cq->ring.pages);
		pthread_spin_destroy(&cq->lock);
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
		qp->state = QPS_RESET;
		qp->scq = d->cqs[sc];
		qp->rcq = d->cqs[rcn];
		qp->rec = gpa_range(d, c->create_qp.rec_gpa, sizeof(*qp->rec));
		if (!qp->rec) {
			free(qp);
			return -EFAULT;
		}
		rc = ring_setup(d, &qp->sq, &c->create_qp.sq);
		if (!rc)
			rc = ring_setup(d, &qp->rq, &c->create_qp.rq);
		if (rc || qp->sq.esz < SHMRDMA_WQE_HDR ||
		    qp->rq.esz < sizeof(struct shmrdma_rq_wqe)) {
			free(qp->sq.pages);
			free(qp->rq.pages);
			free(qp);
			return rc ? rc : -EINVAL;
		}
		pthread_spin_init(&qp->rq_lock, 0);
		qp->sq_cons = RLX(&qp->rec->sq_cons);
		qp->rq_cons = RLX(&qp->rec->rq_cons);
		qp->rnr_retry = 7;
		wlock();
		d->qps[qpn] = qp;
		worker_add_qp(qp);
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
		if (c->modify_qp.mask & SHMRDMA_MQ_DEST) {
			memcpy(qp->dgid, c->modify_qp.dgid, 16);
			qp->dqpn = c->modify_qp.dest_qpn;
			qp->peer_gen = 0;
		}
		if (c->modify_qp.mask & SHMRDMA_MQ_QKEY)
			qp->qkey = c->modify_qp.qkey;
		if (c->modify_qp.mask & SHMRDMA_MQ_RETRY)
			qp->rnr_retry = c->modify_qp.rnr_retry;
		if (c->modify_qp.mask & SHMRDMA_MQ_STATE) {
			uint32_t ns = c->modify_qp.state;

			if (ns == QPS_RESET) {
				/* drop everything outstanding, no completions */
				qp->sq_cons = ACQ(&qp->rec->sq_prod);
				qp->rq_cons = ACQ(&qp->rec->rq_prod);
				REL(&qp->rec->sq_cons, qp->sq_cons);
				REL(&qp->rec->rq_cons, qp->rq_cons);
				qp->fatal_sent = false;
			}
			qp->state = ns;
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

	if (!vq_ready(q))
		return;
	while (q->last_avail != ACQ(&q->avail->idx)) {
		uint16_t head = q->avail->ring[q->last_avail % q->num];
		struct shmrdma_cmd cmd;
		struct shmrdma_resp resp = {0};
		size_t got = 0;
		uint8_t *rp = NULL;
		uint32_t rl = 0;
		uint16_t i = head;
		int guard = 0;

		memset(&cmd, 0, sizeof(cmd));
		q->last_avail++;
		for (;;) {
			struct vring_desc *de = &q->desc[i % q->num];
			uint8_t *p = gpa_range(d, de->addr, de->len);

			if (!p)
				break;
			if (de->flags & VRING_DESC_F_WRITE) {
				if (!rp) {
					rp = p;
					rl = de->len;
				}
			} else if (got < sizeof(cmd)) {
				size_t n = de->len;

				if (n > sizeof(cmd) - got)
					n = sizeof(cmd) - got;
				memcpy((uint8_t *)&cmd + got, p, n);
				got += n;
			}
			if (!(de->flags & VRING_DESC_F_NEXT) || ++guard > 16)
				break;
			i = de->next;
		}
		resp.status = do_cmd(d, &cmd, &resp);
		if (resp.status)
			LOG("dev%d: cmd %u failed %d", d->id, cmd.cmd,
			    resp.status);
		if (rp && rl >= sizeof(resp))
			memcpy(rp, &resp, sizeof(resp));
		vq_push_used(q, head, rp ? sizeof(resp) : 0);
		any = true;
	}
	if (any) {
		FENCE();
		vq_signal(q);
	}
}

/* ---------------------------------------------------------------- */
/* vhost-user message handling                                      */

enum { EP_LISTEN = 1, EP_SOCK, EP_KICK, EP_LISTEN_NET };

struct ep_tag {
	int kind;
	struct vdev *d;
	int vq;
};

static void ep_add(int fd, int kind, struct vdev *d, int vq)
{
	struct ep_tag *t = calloc(1, sizeof(*t));
	struct epoll_event ev = { .events = EPOLLIN };

	t->kind = kind;
	t->d = d;
	t->vq = vq;
	ev.data.ptr = t;
	if (epoll_ctl(g_epfd, EPOLL_CTL_ADD, fd, &ev))
		LOG("epoll add %d: %s", fd, strerror(errno));
}

static void ep_del(int fd)
{
	/* the tag leaks; there are few */
	epoll_ctl(g_epfd, EPOLL_CTL_DEL, fd, NULL);
}

static void unmap_all(struct mapping *m, int n)
{
	for (int i = 0; i < n; i++)
		if (m[i].map_base)
			munmap(m[i].map_base, m[i].map_len);
}

static void vq_stop(struct vdev *d, int idx)
{
	struct vq *q = &d->vq[idx];

	q->started = false;
	if (q->kickfd >= 0) {
		ep_del(q->kickfd);
		close(q->kickfd);
		q->kickfd = -1;
	}
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
	for (int i = 0; i < SHMRDMA_NUM_VQS; i++) {
		vq_stop(d, i);
		if (d->vq[i].callfd >= 0)
			close(d->vq[i].callfd);
	}
	ep_del(d->sock);
	close(d->sock);
	unmap_all(d->map, d->nmap);
	unmap_all(d->old, d->nold);
	free(d->mrs);
	pthread_spin_destroy(&d->evq_lock);
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
	return send(d->sock, &m, sizeof(m.h) + sz, MSG_NOSIGNAL) < 0 ? -1 : 0;
}

static int set_mem_table(struct vdev *d, struct vu_memtab *mt, int *fds, int nfds)
{
	struct mapping nm[VU_MAX_REGIONS];

	if (mt->nregions > VU_MAX_REGIONS || (int)mt->nregions != nfds) {
		LOG("dev%d: bad mem table (%u regions, %d fds)", d->id,
		    mt->nregions, nfds);
		return -1;
	}
	memset(nm, 0, sizeof(nm));
	for (uint32_t i = 0; i < mt->nregions; i++) {
		struct vu_region *r = &mt->r[i];
		size_t len = r->size + r->mmap_offset;
		void *p = mmap(NULL, len, PROT_READ | PROT_WRITE, MAP_SHARED,
			       fds[i], 0);

		close(fds[i]);
		if (p == MAP_FAILED) {
			LOG("dev%d: mmap region %u: %s", d->id, i,
			    strerror(errno));
			unmap_all(nm, i);
			return -1;
		}
		nm[i].gpa = r->gpa;
		nm[i].size = r->size;
		nm[i].uaddr = r->uaddr;
		nm[i].map_base = p;
		nm[i].map_len = len;
		nm[i].hva = (uint8_t *)p + r->mmap_offset;
		LOG("dev%d: region %u gpa 0x%" PRIx64 " size 0x%" PRIx64,
		    d->id, i, r->gpa, r->size);
	}
	wlock();
	if (d->nold + d->nmap <= (int)(sizeof(d->old) / sizeof(d->old[0]))) {
		memcpy(&d->old[d->nold], d->map, d->nmap * sizeof(d->map[0]));
		d->nold += d->nmap;
	} else {
		LOG("dev%d: too many mem table changes, unmapping", d->id);
		unmap_all(d->map, d->nmap);
	}
	memcpy(d->map, nm, sizeof(nm));
	d->nmap = mt->nregions;
	/* vring pointers are re-derived at the next SET_VRING_ADDR */
	wunlock();
	return 0;
}

static int vq_map(struct vdev *d, struct vq *q)
{
	q->desc = uaddr_hva(d, q->desc_ua, 16ull * q->num);
	q->avail = uaddr_hva(d, q->avail_ua, 4 + 2ull * q->num);
	q->used = uaddr_hva(d, q->used_ua, 4 + 8ull * q->num);
	return (q->desc && q->avail && q->used) ? 0 : -1;
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

	n = recvmsg(d->sock, &mh, MSG_CMSG_CLOEXEC);
	if (n <= 0) {
		free(pl);
		return -1;
	}
	if (n != sizeof(h)) {
		LOG("dev%d: short header", d->id);
		free(pl);
		return -1;
	}
	for (struct cmsghdr *cm = CMSG_FIRSTHDR(&mh); cm;
	     cm = CMSG_NXTHDR(&mh, cm)) {
		if (cm->cmsg_level == SOL_SOCKET && cm->cmsg_type == SCM_RIGHTS) {
			nfds = (cm->cmsg_len - CMSG_LEN(0)) / sizeof(int);
			memcpy(fds, CMSG_DATA(cm), nfds * sizeof(int));
		}
	}
	if (h.size > sizeof(*pl)) {
		LOG("dev%d: oversized message %u", d->id, h.size);
		free(pl);
		return -1;
	}
	if (h.size) {
		size_t got = 0;

		while (got < h.size) {
			n = recv(d->sock, pl->raw + got, h.size - got, 0);
			if (n <= 0) {
				free(pl);
				return -1;
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
		break;
	case VU_GET_PROTOCOL_FEATURES: {
		uint64_t f = d->kind == KIND_NET ? PF_REPLY_ACK :
			     PF_MQ | PF_REPLY_ACK;

		rc = vu_reply(d, h.request, &f, 8);
		need_ack = false;
		break;
	}
	case VU_SET_PROTOCOL_FEATURES:
		d->pfeatures = pl->u64;
		break;
	case VU_GET_QUEUE_NUM: {
		uint64_t qn = SHMRDMA_NUM_VQS;

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
		wunlock();
		break;
	case VU_SET_MEM_TABLE:
		if (set_mem_table(d, &pl->mem, fds, nfds))
			ack = 1;
		nfds = 0;
		for (int i = 0; i < SHMRDMA_NUM_VQS; i++)
			if (d->vq[i].num && d->vq[i].desc_ua)
				vq_map(d, &d->vq[i]);
		break;
	case VU_SET_VRING_NUM:
		if (pl->state.index >= SHMRDMA_NUM_VQS) {
			ack = 1;
			break;
		}
		d->vq[pl->state.index].num = pl->state.num;
		break;
	case VU_SET_VRING_ADDR: {
		struct vq *q;

		if (pl->addr.index >= SHMRDMA_NUM_VQS) {
			ack = 1;
			break;
		}
		q = &d->vq[pl->addr.index];
		q->desc_ua = pl->addr.desc;
		q->avail_ua = pl->addr.avail;
		q->used_ua = pl->addr.used;
		if (vq_map(d, q)) {
			LOG("dev%d: vq%u addresses not in guest memory", d->id,
			    pl->addr.index);
			ack = 1;
		}
		break;
	}
	case VU_SET_VRING_BASE:
		if (pl->state.index >= SHMRDMA_NUM_VQS) {
			ack = 1;
			break;
		}
		d->vq[pl->state.index].last_avail = pl->state.num;
		d->vq[pl->state.index].used_idx = pl->state.num;
		break;
	case VU_GET_VRING_BASE: {
		struct vu_state st = pl->state;

		if (st.index >= SHMRDMA_NUM_VQS)
			break;
		wlock();
		if (d->port)
			net_detach(d);
		else if (st.index == SHMRDMA_VQ_CTRL)
			dev_reset_state(d);	/* device stopped/reset */
		pthread_spin_lock(&d->evq_lock);
		vq_stop(d, st.index);
		pthread_spin_unlock(&d->evq_lock);
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

		nfds = 0;
		if (idx >= SHMRDMA_NUM_VQS) {
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
			pthread_spin_lock(&d->evq_lock);
			int old = q->callfd;

			q->callfd = fd;
			pthread_spin_unlock(&d->evq_lock);
			if (old >= 0)
				close(old);
		} else {
			if (q->kickfd >= 0) {
				ep_del(q->kickfd);
				close(q->kickfd);
			}
			q->kickfd = fd;
			if (fd >= 0) {
				bool doorbell = d->kind == KIND_NET ?
					idx == NETQ_TX : idx == SHMRDMA_VQ_DOORBELL;

				if (doorbell) {
					/* workers already serving d watch it */
					for (int i = 0; i < g_nworkers; i++) {
						struct worker *w = &g_workers[i];
						bool mine = false;

						pthread_mutex_lock(&w->mtx);
						for (int j = 0; j < w->nqps; j++)
							mine |= w->qps[j]->dev == d;
						for (int j = 0; j < w->nports; j++)
							mine |= port_dev(w->ports[j]) == d;
						pthread_mutex_unlock(&w->mtx);
						if (mine)
							worker_watch(w, fd);
					}
				} else {
					ep_add(fd, EP_KICK, d, idx);
				}
			}
			q->started = true;
			if (!(d->features & F_PROTOCOL_FEATURES))
				q->enabled = true;
			/* catch anything posted before we listened */
			if (d->kind == KIND_NET) {
				if (d->vq[NETQ_RX].started &&
				    d->vq[NETQ_TX].started) {
					wlock();
					net_attach(d);
					wunlock();
				}
			} else if (idx == SHMRDMA_VQ_CTRL) {
				ctrl_process(d);
			}
		}
		break;
	}
	case VU_SET_VRING_ENABLE:
		if (pl->state.index >= SHMRDMA_NUM_VQS) {
			ack = 1;
			break;
		}
		d->vq[pl->state.index].enabled = pl->state.num;
		if (d->kind == KIND_RDMA &&
		    pl->state.index == SHMRDMA_VQ_CTRL && pl->state.num)
			ctrl_process(d);
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
	for (int i = 0; i < nfds; i++)
		close(fds[i]);
	if (need_ack && rc == 0)
		rc = vu_reply(d, h.request, &ack, 8);
	free(pl);
	return rc;
}

static void accept_conn(int lsock, int kind)
{
	int s = accept4(lsock, NULL, NULL, SOCK_CLOEXEC);
	struct vdev *d;

	if (s < 0)
		return;
	d = calloc(1, sizeof(*d));
	d->id = g_next_dev_id++;
	d->sock = s;
	d->kind = kind;
	for (int i = 0; i < SHMRDMA_NUM_VQS; i++) {
		d->vq[i].kickfd = -1;
		d->vq[i].callfd = -1;
	}
	pthread_spin_init(&d->evq_lock, 0);
	wlock();
	d->next = g_devs;
	g_devs = d;
	wunlock();
	if (kind == KIND_NET)
		net_port_new(d);
	ep_add(s, EP_SOCK, d, 0);
	LOG("dev%d: %s connected", d->id, kind == KIND_NET ? "net" : "rdma");
}

static void on_kick(struct vdev *d, int idx)
{
	uint64_t v;
	struct vq *q = &d->vq[idx];

	if (q->kickfd < 0 || read(q->kickfd, &v, 8) < 0)
		return;
	if (d->kind == KIND_NET) {
		/* TX: frames to send; RX: buffers for a stalled sender */
		if (idx == NETQ_TX)
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
		pthread_spin_lock(&d->evq_lock);
		if (d->irq_pending)
			dev_send_irq(d);
		pthread_spin_unlock(&d->evq_lock);
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

	for (struct netport *p = g_ports; p; p = p->next)
		LOG("dev%d: net %02x:%02x:%02x:%02x:%02x:%02x tx %" PRIu64
		    "/%" PRIu64 " rx %" PRIu64 "/%" PRIu64 " drops %" PRIu64,
		    p->dev->id, p->mac[0], p->mac[1], p->mac[2], p->mac[3],
		    p->mac[4], p->mac[5], p->tx_pkts, p->tx_bytes, p->rx_pkts,
		    p->rx_bytes, p->drops);
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
	__builtin_cpu_init();
	g_have_avx2 = __builtin_cpu_supports("avx2") && !getenv("SHMRDMA_NO_NT");
	signal(SIGPIPE, SIG_IGN);
	signal(SIGUSR1, on_usr1);
	setvbuf(stderr, NULL, _IOLBF, 0);

	g_workers = calloc(g_nworkers, sizeof(*g_workers));
	for (int i = 0; i < g_nworkers; i++) {
		struct worker *w = &g_workers[i];

		w->idx = i;
		pthread_mutex_init(&w->mtx, NULL);
		w->evfd = eventfd(0, EFD_CLOEXEC | EFD_NONBLOCK);
		w->epfd = epoll_create1(EPOLL_CLOEXEC);
		{
			struct epoll_event ev = { .events = EPOLLIN,
						  .data.fd = w->evfd };

			epoll_ctl(w->epfd, EPOLL_CTL_ADD, w->evfd, &ev);
		}
	}
	for (int i = 0; i < g_nworkers; i++)
		pthread_create(&g_workers[i].thr, NULL, worker_main,
			       &g_workers[i]);
	for (int i = 0; i < g_ncopiers; i++) {
		pthread_t t;

		pthread_create(&t, NULL, copier_main, NULL);
	}

	struct sockaddr_un sa = { .sun_family = AF_UNIX };

	strncpy(sa.sun_path, g_sock_path, sizeof(sa.sun_path) - 1);
	unlink(g_sock_path);
	g_lsock = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
	if (bind(g_lsock, (struct sockaddr *)&sa, sizeof(sa)) ||
	    listen(g_lsock, 64)) {
		LOG("listen %s: %s", g_sock_path, strerror(errno));
		return 1;
	}
	chmod(g_sock_path, 0777);
	g_epfd = epoll_create1(EPOLL_CLOEXEC);
	ep_add(g_lsock, EP_LISTEN, NULL, 0);
	if (*g_net_sock_path) {
		struct sockaddr_un na = { .sun_family = AF_UNIX };

		strncpy(na.sun_path, g_net_sock_path, sizeof(na.sun_path) - 1);
		unlink(g_net_sock_path);
		g_lsock_net = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
		if (bind(g_lsock_net, (struct sockaddr *)&na, sizeof(na)) ||
		    listen(g_lsock_net, 64)) {
			LOG("listen %s: %s", g_net_sock_path, strerror(errno));
			return 1;
		}
		chmod(g_net_sock_path, 0777);
		ep_add(g_lsock_net, EP_LISTEN_NET, NULL, 0);
		LOG("net switch on %s", g_net_sock_path);
	}
	LOG("listening on %s, %d workers, %d copiers, spin %" PRIu64 " us",
	    g_sock_path, g_nworkers, g_ncopiers, g_spin_ns / 1000);

	for (;;) {
		struct epoll_event evs[32];
		int n = epoll_wait(g_epfd, evs, 32, 1000);

		if (g_stats) {
			g_stats = 0;
			dump_stats();
		}
		for (int i = 0; i < n; i++) {
			struct ep_tag *t = evs[i].data.ptr;

			switch (t->kind) {
			case EP_LISTEN:
				accept_conn(g_lsock, KIND_RDMA);
				break;
			case EP_LISTEN_NET:
				accept_conn(g_lsock_net, KIND_NET);
				break;
			case EP_SOCK:
				if (evs[i].events & (EPOLLHUP | EPOLLERR) ||
				    handle_msg(t->d)) {
					struct vdev *d = t->d;

					/* stale tags for d's fds may follow */
					for (int j = i + 1; j < n; j++) {
						struct ep_tag *u = evs[j].data.ptr;

						if (u->d == d)
							u->kind = 0;
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
