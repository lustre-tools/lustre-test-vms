// SPDX-License-Identifier: GPL-2.0
/*
 * shmnet: vhost-user-net ports switched by the daemon.
 *
 * Each VM's virtio-net NIC is a port.  A frame the guest transmits is
 * copied straight from its TX buffers into the destination guest's RX
 * buffers: one memcpy, no tap, no host stack.  Offload state (TSO,
 * partial checksum) travels in the virtio-net header, so a 64 KB TCP
 * segment crosses as one copy and is never segmented or summed.
 *
 * Included by shmrdmad.c; shares its vhost-user and worker machinery.
 */

#define VNET_F_CSUM		(1ull << 0)
#define VNET_F_GUEST_CSUM	(1ull << 1)
#define VNET_F_GUEST_TSO4	(1ull << 7)
#define VNET_F_GUEST_TSO6	(1ull << 8)
#define VNET_F_GUEST_ECN	(1ull << 9)
#define VNET_F_HOST_TSO4	(1ull << 11)
#define VNET_F_HOST_TSO6	(1ull << 12)
#define VNET_F_HOST_ECN		(1ull << 13)
#define VNET_F_MRG_RXBUF	(1ull << 15)
#define VNET_F_MQ		(1ull << 22)

#define NET_FEATURES (F_VERSION_1 | F_PROTOCOL_FEATURES | VNET_F_CSUM | \
		      VNET_F_GUEST_CSUM | VNET_F_GUEST_TSO4 | \
		      VNET_F_GUEST_TSO6 | VNET_F_GUEST_ECN | VNET_F_HOST_TSO4 | \
		      VNET_F_HOST_TSO6 | VNET_F_HOST_ECN | VNET_F_MRG_RXBUF | \
		      F_RING_INDIRECT_DESC | F_RING_EVENT_IDX | VNET_F_MQ)

#define VNET_HDR_F_NEEDS_CSUM	1
#define VNET_HDR_GSO_NONE	0
#define VNET_HDR_GSO_TCPV4	1
#define VNET_HDR_GSO_TCPV6	4
#define VNET_HDR_GSO_ECN	0x80
#define VRING_USED_F_NO_NOTIFY	1

/* queue pair qi is RX vq 2qi and TX vq 2qi + 1 */
#define NET_RXQ(qi)	(2 * (qi))
#define NET_TXQ(qi)	(2 * (qi) + 1)

struct vnet_hdr {
	uint8_t flags;
	uint8_t gso_type;
	uint16_t hdr_len;
	uint16_t gso_size;
	uint16_t csum_start;
	uint16_t csum_offset;
	uint16_t num_buffers;
} __attribute__((packed));

struct netport;

/* one TX queue of a port: the unit a worker serves */
struct netq {
	struct netport *p;
	int qi;
	struct worker *w;	/* NULL while the queue is not served */
	uint64_t stall_since;
};

struct netport {
	struct vdev *dev;
	uint8_t mac[6];
	bool mac_known;
	bool attached;		/* on the switch */
	pthread_spinlock_t rx_lock[NET_MAX_QP];
	struct netq q[NET_MAX_QP];
	uint64_t tx_pkts, tx_bytes, rx_pkts, rx_bytes, drops;
	struct netport *next;
};

#define STAT_ADD(x, v)	__atomic_add_fetch(&(x), (v), __ATOMIC_RELAXED)

static struct netport *g_ports;		/* changed under wlock */

static struct vdev *netq_dev(struct netq *nq)
{
	return nq->p->dev;
}
static uint64_t g_net_stall_ns = 2000000;

struct seg {
	uint8_t *p;
	uint32_t len;
};

#define MAX_SEGS 72

/* the readable (or writable) descriptors of a chain */
static int vq_chain(struct vdev *d, struct vq *q, uint16_t head, bool writable,
		    struct seg *segs, int max, uint64_t *total)
{
	struct vring_desc *tab = q->desc;
	uint32_t num = q->num;
	uint16_t i = head;
	int n = 0;

	*total = 0;
	for (uint32_t guard = 0; guard < num; guard++) {
		struct vring_desc *de = &tab[i % num];

		if (de->flags & VRING_DESC_F_INDIRECT) {
			/* one indirect table per chain, at its head */
			if (tab != q->desc || de->len < sizeof(*de) ||
			    de->len % sizeof(*de))
				return -1;
			tab = gpa_range(d, de->addr, de->len);
			if (!tab)
				return -1;
			num = de->len / sizeof(*de);
			i = 0;
			guard = (uint32_t)-1;
			continue;
		}

		bool w = de->flags & VRING_DESC_F_WRITE;

		if (w == writable && de->len) {
			uint8_t *p = gpa_range(d, de->addr, de->len);

			if (!p || n == max)
				return -1;
			segs[n].p = p;
			segs[n].len = de->len;
			*total += de->len;
			n++;
		}
		if (!(de->flags & VRING_DESC_F_NEXT))
			return n;
		i = de->next;
	}
	return -1;
}

struct cursor {
	struct seg *s;
	int n, i;
	uint32_t off;
};

/* copy up to len bytes between cursors; returns bytes copied */
static uint64_t cur_copy(struct cursor *dst, struct cursor *src, uint64_t len)
{
	uint64_t done = 0;

	while (done < len && dst->i < dst->n && src->i < src->n) {
		uint32_t dl = dst->s[dst->i].len - dst->off;
		uint32_t sl = src->s[src->i].len - src->off;
		uint64_t k = len - done;

		if (k > dl)
			k = dl;
		if (k > sl)
			k = sl;
		memcpy(dst->s[dst->i].p + dst->off, src->s[src->i].p + src->off, k);
		done += k;
		dst->off += k;
		src->off += k;
		if (dst->off == dst->s[dst->i].len) {
			dst->i++;
			dst->off = 0;
		}
		if (src->off == src->s[src->i].len) {
			src->i++;
			src->off = 0;
		}
	}
	return done;
}

static void cur_read(struct cursor *src, void *out, uint32_t len)
{
	struct seg s = { .p = out, .len = len };
	struct cursor d = { .s = &s, .n = 1 };

	cur_copy(&d, src, len);
}

static void cur_write(struct cursor *dst, const void *in, uint32_t len)
{
	struct seg s = { .p = (uint8_t *)in, .len = len };
	struct cursor c = { .s = &s, .n = 1 };

	cur_copy(dst, &c, len);
}

enum { DLV_OK, DLV_NOBUF, DLV_DROP };

/*
 * Copy one frame into dp's RX buffers.  src is positioned at the
 * start of the frame.  The cursor is restored on return.
 */
static uint16_t *vq_avail_event(struct vq *q)
{
	return (uint16_t *)&q->used->ring[q->num];
}

/* ask the guest to kick when it next adds RX buffers */
static void net_want_rx_kick(struct vq *q)
{
	if (q->event_idx)
		REL(vq_avail_event(q), q->last_avail);
	else
		REL(&q->used->flags, 0);
	FENCE();
}

/* RX queues the destination guest has running: 1..NET_MAX_QP, or 0 */
static int net_rx_queues(struct netport *dp)
{
	int n = 0;

	while (n < NET_MAX_QP && vq_ready(&dp->dev->vq[NET_RXQ(n)]))
		n++;
	return n;
}

static int net_deliver(struct netport *dp, int rqi, const struct vnet_hdr *th,
		       struct cursor *src0, uint64_t len)
{
	struct vdev *d = dp->dev;
	struct vq *q = &d->vq[NET_RXQ(rqi)];
	struct {
		uint16_t head;
		int first, nsegs;
		uint64_t cap;
	} bufs[64];
	struct seg segs[MAX_SEGS * 2];
	int nb = 0, ns = 0;
	uint64_t need = sizeof(struct vnet_hdr) + len, got = 0;
	struct vnet_hdr h = *th;

	if (d->dead || !vq_ready(q))
		return DLV_DROP;
	if (!(d->features & VNET_F_MRG_RXBUF))
		return DLV_DROP;
	if ((h.gso_type & ~VNET_HDR_GSO_ECN) == VNET_HDR_GSO_TCPV4 &&
	    !(d->features & VNET_F_GUEST_TSO4))
		return DLV_DROP;
	if ((h.gso_type & ~VNET_HDR_GSO_ECN) == VNET_HDR_GSO_TCPV6 &&
	    !(d->features & VNET_F_GUEST_TSO6))
		return DLV_DROP;
	if ((h.flags & VNET_HDR_F_NEEDS_CSUM) &&
	    !(d->features & VNET_F_GUEST_CSUM))
		return DLV_DROP;

	pthread_spin_lock(&dp->rx_lock[rqi]);
	uint16_t ai = ACQ(&q->avail->idx);
	uint16_t idx = q->last_avail;

	while (got < need) {
		uint64_t cap;
		int n;

		if (idx == ai || nb == 64 || ns + MAX_SEGS > MAX_SEGS * 2) {
			if (idx == ai)
				net_want_rx_kick(q);
			pthread_spin_unlock(&dp->rx_lock[rqi]);
			return idx == ai ? DLV_NOBUF : DLV_DROP;
		}
		bufs[nb].head = q->avail->ring[idx % q->num];
		n = vq_chain(d, q, bufs[nb].head, true, segs + ns, MAX_SEGS, &cap);
		if (n <= 0) {
			pthread_spin_unlock(&dp->rx_lock[rqi]);
			return DLV_DROP;
		}
		bufs[nb].first = ns;
		bufs[nb].nsegs = n;
		bufs[nb].cap = cap;
		ns += n;
		got += cap;
		nb++;
		idx++;
	}

	struct cursor dst = { .s = segs, .n = ns };
	struct cursor src = *src0;

	h.num_buffers = nb;
	cur_write(&dst, &h, sizeof(h));
	cur_copy(&dst, &src, len);

	uint64_t left = need;

	for (int i = 0; i < nb; i++) {
		struct vring_used_elem *e =
			&q->used->ring[(q->used_idx + i) % q->num];
		uint64_t l = left < bufs[i].cap ? left : bufs[i].cap;

		e->id = bufs[i].head;
		e->len = l;
		left -= l;
	}
	q->last_avail = idx;
	q->used_idx += nb;
	REL(&q->used->idx, q->used_idx);
	if (!q->event_idx && !q->used->flags)
		REL(&q->used->flags, VRING_USED_F_NO_NOTIFY);
	STAT_ADD(dp->rx_pkts, 1);
	STAT_ADD(dp->rx_bytes, len);
	pthread_spin_unlock(&dp->rx_lock[rqi]);
	FENCE();
	vq_signal(q);
	return DLV_OK;
}

static struct netport *port_by_mac(const uint8_t *mac)
{
	for (struct netport *p = g_ports; p; p = p->next)
		if (p->mac_known && !memcmp(p->mac, mac, 6))
			return p;
	return NULL;
}

/*
 * A hash of the frame's IP addresses and TCP/UDP ports, as RSS would
 * take it, so that every segment of a flow lands on one RX queue and is
 * received in order.  0 for anything else.
 */
static uint32_t net_flow_hash(const struct cursor *frame, uint64_t flen)
{
	uint8_t b[96];
	struct cursor c = *frame;
	uint32_t n = flen < sizeof(b) ? flen : sizeof(b);
	uint32_t h = 0x9e3779b9, l4 = 0, ab = 0;
	uint8_t proto = 0;

	if (n < 34)
		return 0;
	cur_read(&c, b, n);
	uint16_t et = b[12] << 8 | b[13];

	if (et == 0x0800) {
		uint32_t ihl = (b[14] & 15) * 4;

		proto = b[23];
		ab = 26;			/* saddr, daddr: 8 bytes */
		h ^= 8;
		l4 = 14 + ihl;
		for (int i = 0; i < 8; i++)
			h = (h ^ b[ab + i]) * 0x01000193;
	} else if (et == 0x86dd && n >= 54) {
		proto = b[20];
		ab = 22;			/* saddr, daddr: 32 bytes */
		l4 = 54;
		for (int i = 0; i < 32; i++)
			h = (h ^ b[ab + i]) * 0x01000193;
	} else {
		return 0;
	}
	if ((proto == 6 || proto == 17) && l4 + 4 <= n)
		for (uint32_t i = 0; i < 4; i++)
			h = (h ^ b[l4 + i]) * 0x01000193;
	return h ^ (h >> 16);
}

/* deliver to dp on the RX queue its flow hashes to */
static int net_send_to(struct netport *dp, uint32_t hash,
		       const struct vnet_hdr *h, struct cursor *frame,
		       uint64_t flen)
{
	int nq = net_rx_queues(dp);

	if (!nq)
		return DLV_DROP;
	return net_deliver(dp, nq == 1 ? 0 : hash % nq, h, frame, flen);
}

/* process up to budget transmitted frames; returns frames handled */
static int net_tx_process(struct netq *nq, int budget, bool *stalled)
{
	struct netport *p = nq->p;
	struct vdev *d = p->dev;
	struct vq *q = &d->vq[NET_TXQ(nq->qi)];
	int done = 0;

	if (d->dead || !vq_ready(q))
		return 0;
	while (done < budget) {
		uint16_t ai = ACQ(&q->avail->idx);

		if (q->last_avail == ai)
			break;

		uint16_t head = q->avail->ring[q->last_avail % q->num];
		struct seg segs[MAX_SEGS];
		uint64_t total;
		int n = vq_chain(d, q, head, false, segs, MAX_SEGS, &total);
		struct vnet_hdr h;
		uint8_t eth[12];

		if (n > 0 && total >= sizeof(h) + 14) {
			struct cursor c = { .s = segs, .n = n };
			struct cursor frame;
			uint64_t flen = total - sizeof(h);

			cur_read(&c, &h, sizeof(h));
			frame = c;
			cur_read(&c, eth, 12);
			if (!p->mac_known || memcmp(p->mac, eth + 6, 6)) {
				memcpy(p->mac, eth + 6, 6);
				p->mac_known = true;
			}

			int rc = DLV_DROP;
			uint32_t fh = net_flow_hash(&frame, flen);

			if (eth[0] & 1) {
				for (struct netport *o = g_ports; o; o = o->next)
					if (o != p && o->attached)
						net_send_to(o, fh, &h, &frame,
							    flen);
				rc = DLV_OK;
			} else {
				struct netport *dp = port_by_mac(eth);

				if (dp && dp != p && dp->attached) {
					rc = net_send_to(dp, fh, &h, &frame,
							 flen);
				} else if (!dp) {
					for (struct netport *o = g_ports; o;
					     o = o->next)
						if (o != p && o->attached)
							net_send_to(o, fh, &h,
								    &frame, flen);
					rc = DLV_OK;
				}
			}
			if (rc == DLV_NOBUF) {
				uint64_t t = now_ns();

				if (!nq->stall_since)
					nq->stall_since = t;
				if (t - nq->stall_since < g_net_stall_ns) {
					*stalled = true;
					break;
				}
			}
			if (rc != DLV_OK)
				STAT_ADD(p->drops, 1);
			STAT_ADD(p->tx_pkts, 1);
			STAT_ADD(p->tx_bytes, flen);
		} else {
			STAT_ADD(p->drops, 1);
		}
		nq->stall_since = 0;
		q->last_avail++;
		vq_push_used(q, head, 0);
		done++;
	}
	if (done) {
		FENCE();
		vq_signal(q);
	}
	return done;
}

static bool net_has_work(struct netq *nq)
{
	struct vq *q = &nq->p->dev->vq[NET_TXQ(nq->qi)];

	return !nq->p->dev->dead && vq_ready(q) &&
	       q->last_avail != ACQ(&q->avail->idx);
}

static void net_set_notify(struct netq *nq, bool want_kicks)
{
	struct vq *q = &nq->p->dev->vq[NET_TXQ(nq->qi)];

	if (!vq_ready(q))
		return;
	if (q->event_idx) {
		/* a stale avail_event behind avail->idx asks for no kick */
		if (want_kicks)
			REL(vq_avail_event(q), q->last_avail);
	} else {
		REL(&q->used->flags, want_kicks ? 0 : VRING_USED_F_NO_NOTIFY);
	}
}

/* caller holds wlock */
static void netq_serve(struct netq *nq)
{
	struct vdev *d = netq_dev(nq);
	struct worker *best = &g_workers[0];

	if (nq->w || !nq->p->attached ||
	    !vq_ready(&d->vq[NET_TXQ(nq->qi)]))
		return;
	for (int i = 1; i < g_nworkers; i++)
		if (g_workers[i].nqps + g_workers[i].nnq <
		    best->nqps + best->nnq)
			best = &g_workers[i];
	if (best->nnq == best->nqcap) {
		best->nqcap = best->nqcap ? best->nqcap * 2 : 8;
		best->nqs = realloc(best->nqs, best->nqcap * sizeof(*best->nqs));
	}
	best->nqs[best->nnq++] = nq;
	worker_watch(best, d->vq[NET_TXQ(nq->qi)].kickfd);
	nq->w = best;
	/* we poll RX avail when delivering: no RX kicks wanted */
	if (vq_ready(&d->vq[NET_RXQ(nq->qi)]) &&
	    !d->vq[NET_RXQ(nq->qi)].event_idx)
		REL(&d->vq[NET_RXQ(nq->qi)].used->flags,
		    VRING_USED_F_NO_NOTIFY);
	net_set_notify(nq, ACQ(&best->sleeping));
}

/* caller holds wlock */
static void netq_unserve(struct netq *nq)
{
	struct worker *w = nq->w;

	if (!w)
		return;
	for (int i = 0; i < w->nnq; i++)
		if (w->nqs[i] == nq) {
			w->nqs[i] = w->nqs[--w->nnq];
			break;
		}
	nq->w = NULL;
}

/* caller holds wlock */
static void net_attach(struct vdev *d)
{
	struct netport *p = d->port;

	if (p->attached)
		return;
	p->attached = true;
	for (int i = 0; i < NET_MAX_QP; i++)
		netq_serve(&p->q[i]);
	LOG("dev%d: net port up, features 0x%" PRIx64, d->id, d->features);
}

/* caller holds wlock */
static void net_detach(struct vdev *d)
{
	struct netport *p = d->port;

	if (!p->attached)
		return;
	for (int i = 0; i < NET_MAX_QP; i++)
		netq_unserve(&p->q[i]);
	p->attached = false;
	p->mac_known = false;
}

static void net_port_new(struct vdev *d)
{
	struct netport *p = calloc(1, sizeof(*p));

	p->dev = d;
	for (int i = 0; i < NET_MAX_QP; i++) {
		pthread_spin_init(&p->rx_lock[i], 0);
		p->q[i].p = p;
		p->q[i].qi = i;
	}
	d->port = p;
	wlock();
	p->next = g_ports;
	g_ports = p;
	wunlock();
}

/* caller holds wlock */
static void net_port_free(struct vdev *d)
{
	struct netport *p = d->port;

	net_detach(d);
	for (struct netport **pp = &g_ports; *pp; pp = &(*pp)->next)
		if (*pp == p) {
			*pp = p->next;
			break;
		}
	for (int i = 0; i < NET_MAX_QP; i++)
		pthread_spin_destroy(&p->rx_lock[i]);
	free(p);
	d->port = NULL;
}
