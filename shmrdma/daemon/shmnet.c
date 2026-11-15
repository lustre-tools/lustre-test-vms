// SPDX-License-Identifier: GPL-2.0
/*
 * shmnet: vhost-user-net ports switched by the daemon.
 *
 * Each VM's virtio-net NIC is a port.  A frame the guest transmits is
 * copied straight from its TX buffers into the destination guest's RX
 * buffers: one memcpy, no tap, no host stack.  Offload state (TSO,
 * partial checksum) travels in the virtio-net header, so a 64 KB TCP
 * segment crosses as one copy and is never segmented or summed.  A
 * port that did not negotiate receive TSO gets such a frame cut into
 * MSS-sized TCP segments instead, as a NIC without LRO delivers them.
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
/* the largest frame a guest may send: a 64 KB GSO frame and its headers */
#define NET_MAX_FRAME		(65536 + 14 + 4)

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
	/* RX: a hold on this queue ran out; frames for it that find no
	 * buffers drop without a hold until then (rx_lock) */
	uint64_t rx_starved_until;
};

struct netport {
	struct vdev *dev;
	/* mac_key() of the address it sends from, 0 until it sends; one
	 * word, read without g_mac_lock; and when it last sent from it */
	uint64_t mac;
	uint64_t mac_ns;
	bool attached;		/* on the switch */
	plat_spinlock_t rx_lock[NET_MAX_QP];
	struct netq q[NET_MAX_QP];
	uint64_t tx_pkts, tx_bytes, rx_pkts, rx_bytes, drops;
	/* sends that found no RX buffers here, held the sender, and the
	 * drops when the hold ran out; and as the held sender */
	uint64_t rx_holds, rx_hold_ns, rx_hold_drops, tx_holds, tx_hold_ns;
	/* segments cut from GSO frames for this port; frames not cut */
	uint64_t rx_segs, rx_seg_drops;
	/* frames for this port it was not given, flooded copies included */
	uint64_t rx_drops;
	struct netport *next;
};

#define STAT_ADD(x, v)	__atomic_add_fetch(&(x), (v), __ATOMIC_RELAXED)

static struct netport *g_ports;		/* changed under wlock */
static plat_spinlock_t g_mac_lock;	/* claiming a source MAC */

/* a port that has not sent from its MAC for this long may lose it */
#define MAC_HOLD_NS	3000000000ll
/* after a hold for a receiver runs out, none for it for this long */
#define RX_STARVE_NS	100000000ull

static struct vdev *netq_dev(struct netq *nq)
{
	return nq->p->dev;
}

#define MAC_KNOWN	(1ull << 48)

static uint64_t mac_key(const uint8_t *m)
{
	uint64_t k = MAC_KNOWN;

	for (int i = 0; i < 6; i++)
		k |= (uint64_t)m[i] << (8 * i);
	return k;
}

static void mac_bytes(uint64_t k, uint8_t *m)
{
	for (int i = 0; i < 6; i++)
		m[i] = k >> (8 * i);
}
static uint64_t g_net_stall_ns = 2000000;

struct seg {
	uint8_t *p;
	uint32_t len;
};

#define MAX_SEGS 72
/* descriptors a chain may take, the indirect one included */
#define MAX_CHAIN (2 * MAX_SEGS)

/*
 * The readable descriptors of a TX chain, or the writable ones of an
 * RX chain, which may hold nothing else.
 */
static int vq_chain(struct vdev *d, struct vq *q, uint16_t head, bool writable,
		    struct seg *segs, int max, uint64_t *total)
{
	struct vring_desc *tab = q->desc;
	uint32_t num = q->num;
	uint32_t i = head;
	bool indirect = false;
	int n = 0;

	*total = 0;
	/* walked under the receiver's rx_lock: every step counts */
	for (uint32_t guard = 0; guard < MAX_CHAIN; guard++) {
		if (i >= num)
			return -1;

		struct vring_desc de = desc_load(&tab[i]);

		if (de.flags & VRING_DESC_F_INDIRECT) {
			/* one indirect table per chain, no longer than the
			 * queue (virtio 1.x 2.7.5.3.1) */
			if (indirect || de.len < sizeof(de) || (de.addr & 7) ||
			    de.len % sizeof(de) || de.len / sizeof(de) > q->num)
				return -1;
			tab = gpa_range(d, de.addr, de.len);
			if (!tab)
				return -1;
			indirect = true;
			num = de.len / sizeof(de);
			i = 0;
			continue;
		}

		bool w = de.flags & VRING_DESC_F_WRITE;

		if (writable && (!w || !de.len))
			return -1;
		if (w == writable && de.len) {
			uint8_t *p = gpa_range(d, de.addr, de.len);

			if (!p || n == max)
				return -1;
			segs[n].p = p;
			segs[n].len = de.len;
			*total += de.len;
			n++;
		}
		if (!(de.flags & VRING_DESC_F_NEXT))
			return n;
		i = de.next;
	}
	return -1;
}

struct cursor {
	struct seg *s;
	int n, i;
	uint32_t off;
};

/* copy up to len bytes between cursors; returns bytes copied */
static uint64_t cur_copy_nt(struct cursor *dst, struct cursor *src,
			    uint64_t len, bool nt)
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
		copy_bytes(dst->s[dst->i].p + dst->off,
			   src->s[src->i].p + src->off, k, nt);
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

static uint64_t cur_copy(struct cursor *dst, struct cursor *src, uint64_t len)
{
	return cur_copy_nt(dst, src, len, false);
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

/* an avail idx more than the ring ahead: stop the queue until
 * SET_VRING_BASE */
static void vq_set_broken(struct vdev *d, struct vq *q, uint16_t ai)
{
	if (!q->broken)
		GLOG(d, "dev%d: net vq%d avail %u is past %u", d->id,
		    (int)(q - d->vq), ai, q->last_avail);
	q->broken = true;
}

/* an RX buffer that can never be filled would hold the ring's head for
 * good: stop the queue until SET_VRING_BASE, as for a bad avail idx */
static void vq_set_broken_buf(struct vdev *d, struct vq *q, uint16_t head)
{
	if (!q->broken)
		GLOG(d, "dev%d: net vq%d RX buffer %u is not usable", d->id,
		    (int)(q - d->vq), head);
	q->broken = true;
}

static uint16_t *vq_avail_event(struct vq *q)
{
	return (uint16_t *)&q->used->ring[q->num];
}

/* ask the guest to kick when it next adds RX buffers */
static void net_want_rx_kick(struct vq *q)
{
	if (q->event_idx)
		REL(vq_avail_event(q), ACQ(&q->avail->idx));
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

#define RX_MAX_BUFS	512

/* RX buffers taken from one queue and not yet handed back */
struct rxtake {
	struct vdev *d;
	struct vq *q;
	uint16_t ai, idx;
	int nb, ns;
	uint16_t head[RX_MAX_BUFS];
	uint32_t len[RX_MAX_BUFS];
	struct seg segs[RX_MAX_BUFS * 2];
};

/* caller holds the queue's rx_lock */
static void rx_begin(struct rxtake *t, struct vdev *d, struct vq *q)
{
	t->d = d;
	t->q = q;
	t->ai = ACQ(&q->avail->idx);
	t->idx = q->last_avail;
	t->nb = 0;
	t->ns = 0;
}

/* buffers for one frame of need bytes, virtio-net header included */
static int rx_take(struct rxtake *t, uint64_t need, struct cursor *dst,
		   uint16_t *nbufs)
{
	struct vq *q = t->q;
	int fb = t->nb, fs = t->ns;
	uint64_t got = 0;

	if ((uint16_t)(t->ai - q->last_avail) > q->num) {
		vq_set_broken(t->d, q, t->ai);
		return DLV_DROP;
	}
	while (got < need) {
		uint64_t cap;
		int n;

		if (t->idx == t->ai) {
			/* a full ring that is still too small never fits */
			if ((uint16_t)(t->ai - q->last_avail) >= q->num)
				return DLV_DROP;
			return DLV_NOBUF;
		}
		if (t->nb == RX_MAX_BUFS || t->ns + MAX_SEGS > RX_MAX_BUFS * 2)
			return DLV_DROP;
		t->head[t->nb] = RLX(&q->avail->ring[t->idx % q->num]);
		n = vq_chain(t->d, q, t->head[t->nb], true, t->segs + t->ns,
			     MAX_SEGS, &cap);
		if (n <= 0) {
			vq_set_broken_buf(t->d, q, t->head[t->nb]);
			return DLV_DROP;
		}
		t->len[t->nb] = need - got < cap ? need - got : cap;
		t->ns += n;
		t->nb++;
		t->idx++;
		got += cap;
	}
	*dst = (struct cursor){ .s = t->segs + fs, .n = t->ns - fs };
	*nbufs = t->nb - fb;
	return DLV_OK;
}

/* hand every taken buffer to the guest; caller holds rx_lock */
static void rx_put(struct rxtake *t)
{
	struct vq *q = t->q;

	for (int i = 0; i < t->nb; i++) {
		struct vring_used_elem *e =
			&q->used->ring[(q->used_idx + i) % q->num];

		e->id = t->head[i];
		e->len = t->len[i];
	}
	q->last_avail = t->idx;
	q->used_idx += t->nb;
	REL(&q->used->idx, q->used_idx);
	if (!q->event_idx && !q->used->flags)
		REL(&q->used->flags, VRING_USED_F_NO_NOTIFY);
}

/*
 * No RX buffers for a frame; caller holds the queue's rx_lock.  A queue
 * whose last hold ran out drops at once while its avail idx stands.
 */
static int rx_nobuf(struct netport *dp, int rqi, struct rxtake *t)
{
	if (now_ns() < dp->q[rqi].rx_starved_until)
		return DLV_DROP;
	net_want_rx_kick(t->q);
	return DLV_NOBUF;
}

/* a hold for dp ran out: its senders hold no more for a while, so a
 * guest that refills a buffer at a time cannot hold them on every frame */
static void rx_starve(struct netport *dp)
{
	uint64_t until = now_ns() + RX_STARVE_NS;

	for (int i = 0; i < NET_MAX_QP; i++) {
		plat_spin_lock(&dp->rx_lock[i]);
		dp->q[i].rx_starved_until = until;
		plat_spin_unlock(&dp->rx_lock[i]);
	}
}

static int net_deliver_segs(struct netport *dp, int rqi,
			    const struct vnet_hdr *th,
			    const struct cursor *src0, uint64_t len);
static void net_fill_csum(struct cursor frame, uint64_t len,
			  const struct vnet_hdr *h);

#define NET_MAX_SPANS	160

/* the pieces of a len-byte copy between two cursors, or -1 */
static int cur_spans(struct cursor d, struct cursor s, uint64_t len,
		     struct copy_span *out, int max)
{
	int n = 0;

	while (len && d.i < d.n && s.i < s.n) {
		uint64_t dl = d.s[d.i].len - d.off;
		uint64_t sl = s.s[s.i].len - s.off;
		uint64_t k = len < dl ? len : dl;

		if (k > sl)
			k = sl;
		if (n == max)
			return -1;
		out[n].dst = d.s[d.i].p + d.off;
		out[n].src = s.s[s.i].p + s.off;
		out[n].len = k;
		n++;
		len -= k;
		d.off += k;
		s.off += k;
		if (d.off == d.s[d.i].len) {
			d.i++;
			d.off = 0;
		}
		if (s.off == s.s[s.i].len) {
			s.i++;
			s.off = 0;
		}
	}
	return n;
}

/* the frame being sent has more queued behind it on its TX queue */
static __thread bool t_net_backlog;

/*
 * Copy a frame's payload into RX buffers, with streaming stores from
 * g_net_nt_min.  When frames are queued behind it, a frame of two
 * g_net_part_min parts or more is split over the copiers.  Every part
 * is done and fenced on return, so the caller may publish the used
 * ring.
 */
static void net_copy(struct cursor *dst, struct cursor *src, uint64_t len)
{
	uint32_t n = g_ncopiers && t_net_backlog ? len / g_net_part_min : 0;

	if (len < g_net_nt_min) {
		cur_copy(dst, src, len);
		return;
	}
	if (n >= 2) {
		struct copy_span sp[NET_MAX_SPANS];
		struct copy_job j = { .spans = sp, .len = len };

		j.nspans = cur_spans(*dst, *src, len, sp, NET_MAX_SPANS);
		if (j.nspans > 0) {
			j.nparts = n > (uint32_t)g_ncopiers + 1 ?
				   (uint32_t)g_ncopiers + 1 : n;
			cp_run(&j);
			return;
		}
	}
	cur_copy_nt(dst, src, len, true);
	plat_nt_fence();
}

/*
 * Copy one frame into dp's RX buffers.  src is positioned at the
 * start of the frame.  The cursor is restored on return.
 */
static int net_deliver(struct netport *dp, int rqi, const struct vnet_hdr *th,
		       struct cursor *src0, uint64_t len)
{
	struct vdev *d = dp->dev;
	struct vq *q = &d->vq[NET_RXQ(rqi)];
	struct rxtake t;
	struct cursor dst;
	struct vnet_hdr h = *th;
	uint8_t gso = h.gso_type & ~VNET_HDR_GSO_ECN;
	uint16_t nbufs;
	bool fill = false;
	int rc, fd;

	if (d->dead || !vq_ready(q) || q->broken)
		return DLV_DROP;
	if (!(d->features & VNET_F_MRG_RXBUF))
		return DLV_DROP;
	/* only what every receiver handles: TCP segmentation, or none */
	if (gso != VNET_HDR_GSO_NONE && gso != VNET_HDR_GSO_TCPV4 &&
	    gso != VNET_HDR_GSO_TCPV6)
		return DLV_DROP;
	/* the sender's header reaches the receiving kernel: check it */
	if (gso != VNET_HDR_GSO_NONE && !h.gso_size)
		return DLV_DROP;
	if ((h.flags & VNET_HDR_F_NEEDS_CSUM) &&
	    (uint32_t)h.csum_start + h.csum_offset + 2 > len)
		return DLV_DROP;
	if (!(d->features & VNET_F_GUEST_ECN))
		h.gso_type &= ~VNET_HDR_GSO_ECN;
	h.flags &= VNET_HDR_F_NEEDS_CSUM;
	if ((gso == VNET_HDR_GSO_TCPV4 && !(d->features & VNET_F_GUEST_TSO4)) ||
	    (gso == VNET_HDR_GSO_TCPV6 && !(d->features & VNET_F_GUEST_TSO6)))
		return net_deliver_segs(dp, rqi, th, src0, len);
	if ((h.flags & VNET_HDR_F_NEEDS_CSUM) &&
	    !(d->features & VNET_F_GUEST_CSUM)) {
		if (gso != VNET_HDR_GSO_NONE)
			return DLV_DROP;
		fill = true;
		h.flags = 0;
		h.csum_start = h.csum_offset = 0;
	}

	plat_spin_lock(&dp->rx_lock[rqi]);
	rx_begin(&t, d, q);
	rc = rx_take(&t, sizeof(h) + len, &dst, &nbufs);
	h.num_buffers = nbufs;
	if (rc == DLV_NOBUF)
		rc = rx_nobuf(dp, rqi, &t);
	if (rc != DLV_OK) {
		plat_spin_unlock(&dp->rx_lock[rqi]);
		return rc;
	}

	struct cursor src = *src0;
	struct cursor body;

	cur_write(&dst, &h, sizeof(h));
	body = dst;
	net_copy(&dst, &src, len);
	if (fill)
		net_fill_csum(body, len, th);
	rx_put(&t);
	STAT_ADD(dp->rx_pkts, 1);
	STAT_ADD(dp->rx_bytes, len);
	fd = vq_need_signal(q);
	plat_spin_unlock(&dp->rx_lock[rqi]);
	if (fd >= 0)
		plat_notifier_signal(fd);
	return DLV_OK;
}

/* skip len bytes of c */
static void cur_skip(struct cursor *c, uint64_t len)
{
	while (len && c->i < c->n) {
		uint64_t k = c->s[c->i].len - c->off;

		if (k > len)
			k = len;
		len -= k;
		c->off += k;
		if (c->off == c->s[c->i].len) {
			c->i++;
			c->off = 0;
		}
	}
}

/* the next len bytes of c as up to max segments; advances c */
static int cur_slice(struct cursor *c, uint64_t len, struct seg *out, int max)
{
	int n = 0;

	while (len && c->i < c->n && n < max) {
		uint64_t k = c->s[c->i].len - c->off;

		if (k > len)
			k = len;
		out[n].p = c->s[c->i].p + c->off;
		out[n].len = k;
		n++;
		len -= k;
		c->off += k;
		if (c->off == c->s[c->i].len) {
			c->i++;
			c->off = 0;
		}
	}
	return len ? -1 : n;
}

/*
 * Internet checksum.  Sums are taken in host byte order and stored
 * back the same way, which gives network order on either endianness.
 */
static uint64_t csum_add(uint64_t sum, const uint8_t *p, uint32_t n)
{
	uint32_t w;
	uint16_t h;

	for (; n >= 4; p += 4, n -= 4) {
		memcpy(&w, p, 4);
		sum += w;
	}
	if (n >= 2) {
		memcpy(&h, p, 2);
		sum += h;
		p += 2;
		n -= 2;
	}
	if (n) {
		uint8_t b[2] = { *p, 0 };

		memcpy(&h, b, 2);
		sum += h;
	}
	return sum;
}

static uint16_t csum_fold(uint64_t s)
{
	s = (s & 0xffffffff) + (s >> 32);
	s = (s & 0xffffffff) + (s >> 32);
	s = (s & 0xffff) + (s >> 16);
	s = (s & 0xffff) + (s >> 16);
	s = (s & 0xffff) + (s >> 16);
	return s;
}

/* sum of the next len bytes of c, the first at an even offset */
static uint64_t cur_csum(struct cursor c, uint64_t len)
{
	uint64_t sum = 0, pos = 0;

	while (len && c.i < c.n) {
		uint64_t k = c.s[c.i].len - c.off;
		uint16_t f;

		if (k > len)
			k = len;
		f = csum_fold(csum_add(0, c.s[c.i].p + c.off, k));
		if (pos & 1)
			f = (uint16_t)(f << 8 | f >> 8);
		sum += f;
		pos += k;
		len -= k;
		c.off += k;
		if (c.off == c.s[c.i].len) {
			c.i++;
			c.off = 0;
		}
	}
	return sum;
}

/* sum of segments laid end to end, the first at an even offset */
static uint64_t csum_segs(const struct seg *s, int n)
{
	uint64_t sum = 0, pos = 0;

	for (int i = 0; i < n; i++) {
		uint16_t f = csum_fold(csum_add(0, s[i].p, s[i].len));

		if (pos & 1)
			f = (uint16_t)(f << 8 | f >> 8);
		sum += f;
		pos += s[i].len;
	}
	return sum;
}

/*
 * Finish a partial checksum in a delivered frame, as the kernel's
 * skb_checksum_help() does: the sum from csum_start to the end, the
 * pseudo-header sum the sender left in place included.
 */
static void net_fill_csum(struct cursor frame, uint64_t len,
			  const struct vnet_hdr *h)
{
	uint16_t v;

	cur_skip(&frame, h->csum_start);
	v = ~csum_fold(cur_csum(frame, len - h->csum_start));
	if (!v)
		v = 0xffff;
	cur_skip(&frame, h->csum_offset);
	cur_write(&frame, &v, 2);
}

static uint16_t get_be16(const uint8_t *p)
{
	return p[0] << 8 | p[1];
}

static uint32_t get_be32(const uint8_t *p)
{
	return (uint32_t)p[0] << 24 | p[1] << 16 | p[2] << 8 | p[3];
}

static void put_be16(uint8_t *p, uint16_t v)
{
	p[0] = v >> 8;
	p[1] = v;
}

static void put_be32(uint8_t *p, uint32_t v)
{
	p[0] = v >> 24;
	p[1] = v >> 16;
	p[2] = v >> 8;
	p[3] = v;
}

#define TCP_FIN		0x01
#define TCP_PSH		0x08
#define TCP_CWR		0x80
#define NET_HDR_MAX	192

/*
 * Deliver a TCP frame to a port that cannot take it as it is: a GSO
 * frame to one without receive TSO is cut into gso_size segments, and
 * a frame with a partial checksum to one without GUEST_CSUM gets the
 * full checksum.  Every segment is taken from the ring before any is
 * handed over, so a retry after DLV_NOBUF repeats nothing.
 */
static int net_deliver_segs(struct netport *dp, int rqi,
			    const struct vnet_hdr *th,
			    const struct cursor *src0, uint64_t len)
{
	struct vdev *d = dp->dev;
	struct vq *q = &d->vq[NET_RXQ(rqi)];
	uint8_t gso = th->gso_type & ~VNET_HDR_GSO_ECN;
	bool partial = d->features & VNET_F_GUEST_CSUM;
	uint8_t hb[NET_HDR_MAX];
	uint32_t n = len < sizeof(hb) ? len : sizeof(hb);
	uint32_t l3 = 14, l4, hlen;
	struct cursor c = *src0;
	struct rxtake t;
	uint16_t et;
	bool v6;

	cur_read(&c, hb, n);
	if (n < 14)
		goto bad;
	et = get_be16(hb + 12);
	while ((et == 0x8100 || et == 0x88a8) && l3 + 4 <= n) {
		et = get_be16(hb + l3 + 2);
		l3 += 4;
	}
	if (et == 0x0800 && l3 + 20 <= n && hb[l3] >> 4 == 4 &&
	    (hb[l3] & 15) >= 5 && hb[l3 + 9] == 6) {
		v6 = false;
		l4 = l3 + (hb[l3] & 15) * 4;
	} else if (et == 0x86dd && l3 + 40 <= n && hb[l3] >> 4 == 6 &&
		   hb[l3 + 6] == 6) {
		v6 = true;
		l4 = l3 + 40;
	} else {
		goto bad;
	}
	if (gso != VNET_HDR_GSO_NONE &&
	    gso != (v6 ? VNET_HDR_GSO_TCPV6 : VNET_HDR_GSO_TCPV4))
		goto bad;
	if (l4 + 20 > n || hb[l4 + 12] >> 4 < 5)
		goto bad;
	hlen = l4 + (hb[l4 + 12] >> 4) * 4;
	if (hlen > n)
		goto bad;

	uint64_t payload = len - hlen;
	uint64_t mss = gso == VNET_HDR_GSO_NONE ? payload : th->gso_size;
	uint32_t nseg;

	if (!payload)
		nseg = 1;
	else if (!mss)
		goto bad;
	else
		nseg = (payload + mss - 1) / mss;
	if (nseg > RX_MAX_BUFS)
		goto bad;

	uint32_t seq0 = get_be32(hb + l4 + 4);
	uint16_t id0 = get_be16(hb + l3 + 4);
	uint64_t off = 0, bytes = 0;
	int rc = DLV_OK;

	c = *src0;
	cur_skip(&c, hlen);
	plat_spin_lock(&dp->rx_lock[rqi]);
	rx_begin(&t, d, q);
	for (uint32_t i = 0; i < nseg; i++) {
		uint64_t sl = payload - off < mss ? payload - off : mss;
		uint32_t tl = hlen - l4 + sl;
		struct vnet_hdr vh = { 0 };
		uint8_t sh[NET_HDR_MAX];
		struct seg ps[MAX_SEGS + 1];
		struct cursor dst;
		uint64_t sum;
		uint16_t v, nbufs;
		int np;

		memcpy(sh, hb, hlen);
		if (v6) {
			uint8_t ph[8] = { tl >> 24, tl >> 16, tl >> 8, tl,
					  0, 0, 0, 6 };

			put_be16(sh + l3 + 4, l4 - l3 - 40 + tl);
			sum = csum_add(csum_add(0, sh + l3 + 8, 32), ph, 8);
		} else {
			uint8_t ph[4] = { 0, 6, tl >> 8, tl };

			put_be16(sh + l3 + 2, l4 - l3 + tl);
			put_be16(sh + l3 + 4, id0 + i);
			sh[l3 + 10] = sh[l3 + 11] = 0;
			v = ~csum_fold(csum_add(0, sh + l3, l4 - l3));
			memcpy(sh + l3 + 10, &v, 2);
			sum = csum_add(csum_add(0, sh + l3 + 12, 8), ph, 4);
		}
		put_be32(sh + l4 + 4, seq0 + (uint32_t)off);
		if (i)
			sh[l4 + 13] &= ~TCP_CWR;
		if (i + 1 < nseg)
			sh[l4 + 13] &= ~(TCP_FIN | TCP_PSH);
		sh[l4 + 16] = sh[l4 + 17] = 0;

		ps[0].p = sh;
		ps[0].len = hlen;
		np = cur_slice(&c, sl, ps + 1, MAX_SEGS);
		if (np < 0) {
			rc = DLV_DROP;
			break;
		}
		if (partial) {
			v = csum_fold(sum);
			vh.flags = VNET_HDR_F_NEEDS_CSUM;
			vh.csum_start = l4;
			vh.csum_offset = 16;
		} else {
			sum += csum_add(0, sh + l4, hlen - l4);
			v = ~csum_fold(sum + csum_segs(ps + 1, np));
		}
		memcpy(sh + l4 + 16, &v, 2);

		rc = rx_take(&t, sizeof(vh) + hlen + sl, &dst, &nbufs);
		if (rc != DLV_OK)
			break;
		vh.num_buffers = nbufs;

		struct cursor pc = { .s = ps, .n = np + 1 };

		cur_write(&dst, &vh, sizeof(vh));
		cur_copy(&dst, &pc, hlen + sl);
		off += sl;
		bytes += hlen + sl;
	}
	if (rc == DLV_NOBUF)
		rc = rx_nobuf(dp, rqi, &t);
	if (rc != DLV_OK) {
		plat_spin_unlock(&dp->rx_lock[rqi]);
		if (rc == DLV_DROP)
			STAT_ADD(dp->rx_seg_drops, 1);
		return rc;
	}
	rx_put(&t);
	STAT_ADD(dp->rx_pkts, nseg);
	STAT_ADD(dp->rx_bytes, bytes);
	if (gso != VNET_HDR_GSO_NONE)
		STAT_ADD(dp->rx_segs, nseg);
	int fd = vq_need_signal(q);

	plat_spin_unlock(&dp->rx_lock[rqi]);
	if (fd >= 0)
		plat_notifier_signal(fd);
	return DLV_OK;
bad:
	STAT_ADD(dp->rx_seg_drops, 1);
	return DLV_DROP;
}

static struct netport *port_by_mac(const uint8_t *mac)
{
	uint64_t k = mac_key(mac);

	for (struct netport *p = g_ports; p; p = p->next)
		if (RLX(&p->mac) == k)
			return p;
	return NULL;
}

/*
 * p sends from mac: learn it, unless another port on the switch holds
 * it and has sent from it lately.  A port that went quiet loses it, as
 * a virtual MAC moves on failover.
 */
static bool net_claim_mac(struct netport *p, const uint8_t *mac)
{
	uint64_t k = mac_key(mac), t = now_ns();
	bool ok = true;

	if (RLX(&p->mac) == k) {
		if ((int64_t)(t - RLX(&p->mac_ns)) > MAC_HOLD_NS / 4)
			REL(&p->mac_ns, t);
		return true;
	}
	plat_spin_lock(&g_mac_lock);
	for (struct netport *o = g_ports; o; o = o->next) {
		if (o == p || RLX(&o->mac) != k)
			continue;
		if (o->attached &&
		    (int64_t)(t - RLX(&o->mac_ns)) < MAC_HOLD_NS)
			ok = false;
		else
			REL(&o->mac, 0);
	}
	if (ok) {
		REL(&p->mac_ns, t);
		REL(&p->mac, k);
	}
	plat_spin_unlock(&g_mac_lock);
	return ok;
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

	if (d->dead || !vq_ready(q) || q->broken)
		return 0;
	while (done < budget) {
		uint16_t ai = ACQ(&q->avail->idx);

		if (q->last_avail == ai)
			break;
		if ((uint16_t)(ai - q->last_avail) > q->num) {
			vq_set_broken(d, q, ai);
			break;
		}

		uint16_t head = RLX(&q->avail->ring[q->last_avail % q->num]);
		struct seg segs[MAX_SEGS];
		uint64_t total;
		int n = vq_chain(d, q, head, false, segs, MAX_SEGS, &total);
		/* without VERSION_1 or MRG_RXBUF the header has no num_buffers */
		uint32_t hlen = d->features & (F_VERSION_1 | VNET_F_MRG_RXBUF) ?
				sizeof(struct vnet_hdr) : 10;
		struct vnet_hdr h = { 0 };
		uint8_t eth[12];

		t_net_backlog = (uint16_t)(ai - q->last_avail) > 1;

		if (n > 0 && total >= hlen + 14 && total <= hlen + NET_MAX_FRAME) {
			struct cursor c = { .s = segs, .n = n };
			struct cursor frame;
			uint64_t flen = total - hlen;

			cur_read(&c, &h, hlen);
			frame = c;
			cur_read(&c, eth, 12);
			if (!net_claim_mac(p, eth + 6))
				goto drop;

			int rc = DLV_DROP;
			struct netport *dp = NULL;
			uint32_t fh = net_flow_hash(&frame, flen);

			dp = eth[0] & 1 ? NULL : port_by_mac(eth);
			if (dp && dp != p && dp->attached) {
				rc = net_send_to(dp, fh, &h, &frame, flen);
				if (rc == DLV_DROP)
					STAT_ADD(dp->rx_drops, 1);
			} else if (!dp) {
				/* broadcast, multicast, or no port has it */
				for (struct netport *o = g_ports; o; o = o->next)
					if (o != p && o->attached &&
					    net_send_to(o, fh, &h, &frame,
							flen) != DLV_OK)
						STAT_ADD(o->rx_drops, 1);
				rc = DLV_OK;
			}
			if (rc == DLV_NOBUF) {
				uint64_t t = now_ns();

				if (!nq->stall_since) {
					nq->stall_since = t;
					STAT_ADD(p->tx_holds, 1);
					STAT_ADD(dp->rx_holds, 1);
				}
				if (t - nq->stall_since < g_net_stall_ns) {
					*stalled = true;
					break;
				}
				STAT_ADD(dp->rx_hold_drops, 1);
				STAT_ADD(dp->rx_drops, 1);
				rx_starve(dp);
			}
			if (nq->stall_since) {
				uint64_t held = now_ns() - nq->stall_since;

				STAT_ADD(p->tx_hold_ns, held);
				if (dp)
					STAT_ADD(dp->rx_hold_ns, held);
			}
			if (rc != DLV_OK)
				STAT_ADD(p->drops, 1);
			STAT_ADD(p->tx_pkts, 1);
			STAT_ADD(p->tx_bytes, flen);
		} else {
drop:
			STAT_ADD(p->drops, 1);
		}
		nq->stall_since = 0;
		q->last_avail++;
		vq_push_used(q, head, 0);
		done++;
	}
	if (done)
		vq_signal(q);
	return done;
}

static bool net_has_work(struct netq *nq)
{
	struct vq *q = &nq->p->dev->vq[NET_TXQ(nq->qi)];

	return !nq->p->dev->dead && vq_ready(q) && !q->broken &&
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
		int cap = best->nqcap ? best->nqcap * 2 : 8;
		struct netq **nqs = realloc(best->nqs, cap * sizeof(*nqs));

		if (!nqs) {
			LOG("dev%d: no memory to serve net queue %d", d->id,
			    nq->qi);
			return;
		}
		best->nqs = nqs;
		best->nqcap = cap;
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

	nq->stall_since = 0;
	if (!w)
		return;
	for (int i = 0; i < w->nnq; i++)
		if (w->nqs[i] == nq) {
			w->nqs[i] = w->nqs[--w->nnq];
			break;
		}
	worker_unwatch(w, netq_dev(nq)->vq[NET_TXQ(nq->qi)].kickfd);
	nq->w = NULL;
}

/* caller holds wlock */
static void net_attach(struct vdev *d)
{
	struct netport *p = d->port;

	if (p->attached)
		return;
	p->attached = true;
	/* a hold or a starved RX queue from before the detach is over */
	for (int i = 0; i < NET_MAX_QP; i++) {
		p->q[i].stall_since = 0;
		p->q[i].rx_starved_until = 0;
		netq_serve(&p->q[i]);
	}
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
	REL(&p->mac, 0);
}

static int net_port_new(struct vdev *d)
{
	struct netport *p = calloc(1, sizeof(*p));

	if (!p)
		return -1;
	p->dev = d;
	for (int i = 0; i < NET_MAX_QP; i++) {
		plat_spin_init(&p->rx_lock[i]);
		p->q[i].p = p;
		p->q[i].qi = i;
	}
	d->port = p;
	wlock();
	p->next = g_ports;
	g_ports = p;
	wunlock();
	return 0;
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
		plat_spin_destroy(&p->rx_lock[i]);
	free(p);
	d->port = NULL;
}

/*
 * Every port's counters, one line each, in <net socket>.stats.  Run
 * on the main thread, which alone changes the port list.
 */
static void net_write_stats(void)
{
	/* a socket path fits in sun_path */
	char path[160], tmp[168];
	FILE *f;
	int fd;

	if (!*g_net_sock_path ||
	    snprintf(path, sizeof(path), "%s.stats", g_net_sock_path) >=
		    (int)sizeof(path) ||
	    snprintf(tmp, sizeof(tmp), "%s.tmp", path) >= (int)sizeof(tmp))
		return;
	/* the socket's directory may be another user's to write in too */
	unlink(tmp);
	fd = open(tmp, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC,
		  0600);
	if (fd < 0)
		return;
	f = fdopen(fd, "w");
	if (!f) {
		close(fd);
		unlink(tmp);
		return;
	}
	for (struct netport *p = g_ports; p; p = p->next) {
		uint64_t ft = p->dev->features;
		uint8_t m[6];

		mac_bytes(RLX(&p->mac), m);
		fprintf(f, "dev=%d mac=%02x:%02x:%02x:%02x:%02x:%02x "
			"attached=%d queues=%d guest_tso4=%d guest_tso6=%d "
			"guest_csum=%d",
			p->dev->id, m[0], m[1], m[2], m[3], m[4], m[5],
			p->attached,
			p->attached ? net_rx_queues(p) : 0,
			!!(ft & VNET_F_GUEST_TSO4), !!(ft & VNET_F_GUEST_TSO6),
			!!(ft & VNET_F_GUEST_CSUM));
#define STAT_OUT(x) fprintf(f, " " #x "=%" PRIu64, RLX(&p->x))
		STAT_OUT(tx_pkts);
		STAT_OUT(tx_bytes);
		STAT_OUT(rx_pkts);
		STAT_OUT(rx_bytes);
		STAT_OUT(drops);
		STAT_OUT(rx_holds);
		STAT_OUT(rx_hold_ns);
		STAT_OUT(rx_hold_drops);
		STAT_OUT(tx_holds);
		STAT_OUT(tx_hold_ns);
		STAT_OUT(rx_segs);
		STAT_OUT(rx_seg_drops);
		STAT_OUT(rx_drops);
#undef STAT_OUT
		fputc('\n', f);
	}
	if (fclose(f) || rename(tmp, path))
		unlink(tmp);
}
