// SPDX-License-Identifier: GPL-2.0
/*
 * Link faults: what a switch port or cable does to a link, set from the
 * control socket (-C) and applied to RDMA devices and shmnet ports.
 * Included by shmrdmad.c.
 *
 * A rule selects a link by IP address (the RDMA device holding that
 * address as a GID), by MAC (the shmnet port sending from it, and the
 * RDMA device holding its link-local GID), or both.  A transfer from A
 * to B sees both ends' rules: down at either end, the delays and loss
 * added, A's send rate and B's receive rate.  A flooded frame (broadcast,
 * multicast, unknown unicast) sees only A's delay: each copy is dropped
 * for B's down, loss or rate, but is not held for B's delay.
 *
 * Down is silent, as a dead switch port is: an RC send waits as on a
 * real HCA retrying, and fails with RETRY_EXC_ERR once the QP's retries
 * would run out (4.096 us << timeout, retry_cnt + 1 times), unless the
 * link comes back first.  UD sends (the CM's MADs) and shmnet frames
 * are dropped.  A lost RC message is sent again after the QP's ack
 * timeout, and its retries can run out the same way.
 *
 * Rules change only under wlock, which no worker holds a QP or a queue
 * across, so a worker may use a rule it looked up for as long as it
 * holds its own mutex.
 */

#define FAULT_MAX	64

/* what a QP is assumed to use when its driver does not say: the ack
 * timeout rdma_cm gives a RoCE QP, and IB's largest retry count */
#define DEF_ACK_TIMEOUT	17
#define DEF_RETRY_CNT	7

struct bucket {
	plat_spinlock_t lock;
	int64_t tokens;		/* bytes; a send may take it below 0 */
	uint64_t last;
};

struct fault {
	bool used;
	bool has_ip, has_mac;
	uint8_t gid[16];	/* ip= as a GID */
	uint64_t mac;		/* mac_key() of mac= */
	uint8_t ll_gid[16];	/* the link-local GID of mac= */
	bool down;
	uint64_t delay_ns;
	uint32_t loss_ppm;
	uint64_t rate;		/* bytes per second each way, 0 unlimited */
	uint64_t retry_ns;	/* down: RC retries run out after this */
	struct bucket tx, rx;
	uint64_t drops;		/* UD sends and frames dropped */
	uint64_t lost;		/* RC messages lost and sent again */
	uint64_t fails;		/* RC sends failed: retries ran out */
	uint64_t since;		/* when it was set, CLOCK_MONOTONIC */
};

static struct fault g_faults[FAULT_MAX];
static int g_nfaults;		/* rules in use, under wlock */
static const char *g_ctl_path;
static int g_lsock_ctl = -1;

static uint64_t dev_port_mac(struct vdev *d);

static __thread uint64_t t_rng;

static uint32_t rng_ppm(void)
{
	if (!t_rng)
		t_rng = now_ns() | 1;
	t_rng ^= t_rng << 13;
	t_rng ^= t_rng >> 7;
	t_rng ^= t_rng << 17;
	return (uint32_t)((t_rng >> 11) % 1000000);
}

static bool fault_matches(const struct fault *f, struct vdev *d)
{
	if (d->kind == KIND_NET)
		return f->has_mac && dev_port_mac(d) == f->mac;
	return (f->has_ip && dev_has_gid(d, f->gid)) ||
	       (f->has_mac && dev_has_gid(d, f->ll_gid));
}

/* d's rule, or NULL; from a worker holding its mutex, or under wlock */
static struct fault *dev_fault(struct vdev *d)
{
	uint64_t gen;
	struct fault *f = NULL;

	if (!d || !RLX(&g_nfaults))
		return NULL;
	gen = ACQ(&g_fault_gen);
	plat_spin_lock(&d->fault_lock);
	if (d->fault_gen == gen) {
		f = d->fault;
		plat_spin_unlock(&d->fault_lock);
		return f;
	}
	plat_spin_unlock(&d->fault_lock);
	for (int i = 0; i < FAULT_MAX && !f; i++)
		if (g_faults[i].used && fault_matches(&g_faults[i], d))
			f = &g_faults[i];
	/* workers looking up a peer race: keep the newest */
	plat_spin_lock(&d->fault_lock);
	if (ACQ(&g_fault_gen) == gen) {
		d->fault = f;
		d->fault_gen = gen;
	}
	plat_spin_unlock(&d->fault_lock);
	return f;
}

/* refill b; caller holds its lock */
static void bucket_refill(struct bucket *b, uint64_t rate, uint64_t t)
{
	int64_t burst = rate / 1000 > 65536 ? rate / 1000 : 65536;

	if (!b->last) {
		b->tokens = burst;
		b->last = t;
	} else if (t > b->last) {
		unsigned __int128 add = (unsigned __int128)(t - b->last) * rate;

		add /= 1000000000u;
		if (add >= (unsigned __int128)(burst - b->tokens)) {
			b->tokens = burst;
			b->last = t;
		} else if (add) {
			/* keep the fraction of a byte for the next refill */
			b->tokens += (int64_t)add;
			b->last += (uint64_t)(add * 1000000000u / rate);
		}
	}
}

static void bucket_charge(struct bucket *b, uint64_t n)
{
	b->tokens -= (int64_t)(n > INT64_MAX / 2 ? INT64_MAX / 2 : n);
}

/* n bytes from the end with rule s to the one with rule d, or false */
static bool fault_rate_take(struct fault *s, struct fault *d, uint64_t n,
			    uint64_t t)
{
	struct bucket *tx = s && s->rate ? &s->tx : NULL;
	struct bucket *rx = d && d->rate ? &d->rx : NULL;
	bool ok = true;

	/* checked and charged together; tx locks before rx locks */
	if (tx) {
		plat_spin_lock(&tx->lock);
		bucket_refill(tx, s->rate, t);
		ok = tx->tokens > 0;
	}
	if (rx && ok) {
		plat_spin_lock(&rx->lock);
		bucket_refill(rx, d->rate, t);
		ok = rx->tokens > 0;
		if (ok)
			bucket_charge(rx, n);
		plat_spin_unlock(&rx->lock);
	}
	if (tx) {
		if (ok)
			bucket_charge(tx, n);
		plat_spin_unlock(&tx->lock);
	}
	return ok;
}

static uint32_t fault_loss(const struct fault *a, const struct fault *b)
{
	uint32_t p = (a ? a->loss_ppm : 0) + (b ? b->loss_ppm : 0);

	return p > 1000000 ? 1000000 : p;
}

static bool fault_down(const struct fault *a, const struct fault *b)
{
	return (a && a->down) || (b && b->down);
}

static uint64_t fault_delay(const struct fault *a, const struct fault *b)
{
	return (a ? a->delay_ns : 0) + (b ? b->delay_ns : 0);
}

/* ---------------------------------------------------------------- */
/* RDMA                                                             */

static uint64_t qp_ack_ns(const struct qp *qp)
{
	uint32_t to = qp->timeout_known ? qp->timeout : DEF_ACK_TIMEOUT;

	/* 0 is an infinite timeout */
	if (!to || to > 31)
		return UINT64_MAX / 64;
	return 4096ull << to;
}

static uint64_t qp_retry_ns(const struct qp *qp, const struct fault *a,
			    const struct fault *b)
{
	uint64_t o = 0;

	if (a && a->retry_ns > o)
		o = a->retry_ns;
	if (b && b->retry_ns > o)
		o = b->retry_ns;
	if (o)
		return o;
	return qp_ack_ns(qp) *
	       ((qp->timeout_known ? qp->retry_cnt : DEF_RETRY_CNT) + 1);
}

static void fault_qp_reset(struct qp *qp)
{
	qp->gated = false;
	qp->blk_since = 0;
	qp->loss_until = 0;
	qp->loss_tries = 0;
	qp->stamp_to = qp->sq_cons;
}

/* when the WQE at the head of qp's send queue was first seen */
static uint64_t qp_head_stamp(struct qp *qp, uint32_t prod, uint64_t t)
{
	uint32_t mask = qp->sq.entries - 1;

	if (!qp->stamp) {
		qp->stamp = calloc(qp->sq.entries, sizeof(*qp->stamp));
		if (!qp->stamp)
			return t;
		qp->stamp_to = qp->sq_cons;
	}
	if (qp->stamp_to - qp->sq_cons > qp->sq.entries ||
	    prod - qp->stamp_to > qp->sq.entries)
		qp->stamp_to = qp->sq_cons;
	while (qp->stamp_to != prod)
		qp->stamp[qp->stamp_to++ & mask] = t;
	return qp->stamp[qp->sq_cons & mask];
}

/*
 * May the network operation at the head of qp's send queue go now?
 * len is the bytes it moves; pd the peer's device, if known.  held: it
 * passed the gate already and waits on its peer, so only down applies.
 */
static int fault_gate(struct qp *qp, struct vdev *pd, bool ud, bool read,
		      uint64_t len, uint32_t prod, bool held)
{
	struct fault *fs = dev_fault(qp->dev), *fd = dev_fault(pd);
	uint64_t t, delay;
	uint32_t loss;

	if (!fs && !fd) {
		if (qp->gated || qp->blk_since || qp->loss_until)
			fault_qp_reset(qp);
		return FG_GO;
	}
	t = now_ns();
	if (fault_down(fs, fd)) {
		if (ud) {
			STAT_ADD((fs && fs->down ? fs : fd)->drops, 1);
			return FG_DROP;
		}
		if (!qp->blk_since)
			qp->blk_since = t;
		if (t - qp->blk_since < qp_retry_ns(qp, fs, fd))
			return FG_WAIT;
		STAT_ADD((fs && fs->down ? fs : fd)->fails, 1);
		fault_qp_reset(qp);
		return FG_FAIL;
	}
	qp->blk_since = 0;
	if (held)
		return FG_GO;
	loss = fault_loss(fs, fd);
	/* as with no rule at all: a cleared loss leaves no retries */
	if (!loss) {
		qp->loss_until = 0;
		qp->loss_tries = 0;
	}
	delay = fault_delay(fs, fd);
	if (delay && t < qp_head_stamp(qp, prod, t) + delay)
		return FG_WAIT;
	if (qp->loss_until && t < qp->loss_until)
		return FG_WAIT;
	if (qp->loss_tries >
	    (qp->timeout_known ? qp->retry_cnt : DEF_RETRY_CNT)) {
		struct fault *f = fs && fs->loss_ppm ? fs : fd;

		if (f)
			STAT_ADD(f->fails, 1);
		fault_qp_reset(qp);
		return FG_FAIL;
	}
	if (!(read ? fault_rate_take(fd, fs, len, t) :
		     fault_rate_take(fs, fd, len, t)))
		return FG_WAIT;
	if (loss && rng_ppm() < loss) {
		struct fault *f = fs && fs->loss_ppm ? fs : fd;

		if (ud) {
			STAT_ADD(f->drops, 1);
			return FG_DROP;
		}
		STAT_ADD(f->lost, 1);
		qp->loss_tries++;
		/* from when it went, or the last timeout if a poll was late */
		qp->loss_until = (qp->loss_until > t ? qp->loss_until : t) +
				 qp_ack_ns(qp);
		return FG_WAIT;
	}
	qp->loss_until = 0;
	qp->loss_tries = 0;
	return FG_GO;
}

/* ---------------------------------------------------------------- */
/* shmnet                                                           */

/* when the frame at avail position ai of nq's TX queue was first seen */
static uint64_t netq_head_stamp(struct netq *nq, struct vq *q, uint16_t ai,
				uint64_t t)
{
	if (nq->stamp_n != q->num) {
		free(nq->stamp);
		nq->stamp = calloc(q->num, sizeof(*nq->stamp));
		nq->stamp_n = nq->stamp ? q->num : 0;
		if (!nq->stamp)
			return t;
		nq->stamp_to = q->last_avail;
	}
	if ((uint16_t)(nq->stamp_to - q->last_avail) > q->num ||
	    (uint16_t)(ai - nq->stamp_to) > q->num)
		nq->stamp_to = q->last_avail;
	while (nq->stamp_to != ai)
		nq->stamp[nq->stamp_to++ % q->num] = t;
	return nq->stamp[q->last_avail % q->num];
}

/*
 * May the frame at the head of nq go now, to dp (NULL when flooded)?
 * FG_DROP when a down link or a loss takes it.  held: as for fault_gate().
 */
static int fault_net_gate(struct netq *nq, struct netport *dp, uint64_t flen,
			  uint16_t ai, bool held)
{
	struct fault *fs, *fd;
	struct vq *q = &nq->p->dev->vq[NET_TXQ(nq->qi)];
	uint64_t t, delay;
	uint32_t loss;

	/* with no delay, stamping restarts at the head when one comes:
	 * a cursor left behind could match again once the index wraps */
	if (!RLX(&g_nfaults)) {
		nq->stamp_to = q->last_avail;
		return FG_GO;
	}
	fs = dev_fault(nq->p->dev);
	fd = dp ? dev_fault(dp->dev) : NULL;
	if (!fs && !fd) {
		nq->stamp_to = q->last_avail;
		return FG_GO;
	}
	if (fault_down(fs, fd)) {
		STAT_ADD((fs && fs->down ? fs : fd)->drops, 1);
		return FG_DROP;
	}
	if (held)
		return FG_GO;
	t = now_ns();
	delay = fault_delay(fs, fd);
	if (!delay)
		nq->stamp_to = q->last_avail;
	else if (t < netq_head_stamp(nq, q, ai, t) + delay)
		return FG_WAIT;
	if (!fault_rate_take(fs, fd, flen, t))
		return FG_WAIT;
	loss = fault_loss(fs, fd);
	if (loss && rng_ppm() < loss) {
		struct fault *f = fs && fs->loss_ppm ? fs : fd;

		STAT_ADD(f->drops, 1);
		return FG_DROP;
	}
	return FG_GO;
}

/*
 * A flooded copy of flen bytes for o that o's own link drops.  The copy
 * is not held for o's delay, and is dropped when o's rate has no room.
 */
static bool fault_net_drop_copy(struct netport *o, uint64_t flen)
{
	struct fault *f;

	if (!RLX(&g_nfaults) || !(f = dev_fault(o->dev)))
		return false;
	if (f->down || (f->loss_ppm && rng_ppm() < f->loss_ppm) ||
	    (f->rate && !fault_rate_take(NULL, f, flen, now_ns()))) {
		STAT_ADD(f->drops, 1);
		return true;
	}
	return false;
}

/* ---------------------------------------------------------------- */
/* control socket                                                   */

static bool parse_ip(const char *s, uint8_t *gid)
{
	struct in_addr a4;

	memset(gid, 0, 16);
	if (inet_pton(AF_INET, s, &a4) == 1) {
		gid[10] = gid[11] = 0xff;
		memcpy(gid + 12, &a4, 4);
		return true;
	}
	return inet_pton(AF_INET6, s, gid) == 1;
}

static bool parse_mac(const char *s, uint8_t *m)
{
	char tail;

	return sscanf(s, "%2hhx:%2hhx:%2hhx:%2hhx:%2hhx:%2hhx%c", &m[0], &m[1],
		      &m[2], &m[3], &m[4], &m[5], &tail) == 6;
}

static void ll_gid_of(const uint8_t *m, uint8_t *g)
{
	memset(g, 0, 16);
	g[0] = 0xfe;
	g[1] = 0x80;
	g[8] = m[0] ^ 2;
	g[9] = m[1];
	g[10] = m[2];
	g[11] = 0xff;
	g[12] = 0xfe;
	g[13] = m[3];
	g[14] = m[4];
	g[15] = m[5];
}

static bool parse_num(const char *s, double lo, double hi, double *out)
{
	char *end;
	double v = strtod(s, &end);

	if (end == s || *end || !(v >= lo && v <= hi))
		return false;
	*out = v;
	return true;
}

struct ctl_out {
	char buf[16384];
	size_t len;
};

__attribute__((format(printf, 2, 3)))
static void ctl_printf(struct ctl_out *o, const char *fmt, ...)
{
	va_list ap;
	int n;

	if (o->len >= sizeof(o->buf))
		return;
	va_start(ap, fmt);
	n = vsnprintf(o->buf + o->len, sizeof(o->buf) - o->len, fmt, ap);
	va_end(ap);
	if (n > 0)
		o->len += n;
	if (o->len > sizeof(o->buf))
		o->len = sizeof(o->buf);
}

static void fault_describe(struct ctl_out *o, struct fault *f)
{
	char ip[64] = "-";
	uint8_t m[6];

	static const uint8_t v4pfx[12] = { [10] = 0xff, [11] = 0xff };

	if (f->has_ip && !memcmp(f->gid, v4pfx, 12))
		inet_ntop(AF_INET, f->gid + 12, ip, sizeof(ip));
	else if (f->has_ip)
		inet_ntop(AF_INET6, f->gid, ip, sizeof(ip));
	mac_bytes(f->mac, m);
	ctl_printf(o, "link ip=%s mac=", ip);
	if (f->has_mac)
		ctl_printf(o, "%02x:%02x:%02x:%02x:%02x:%02x", m[0], m[1],
			   m[2], m[3], m[4], m[5]);
	else
		ctl_printf(o, "-");
	ctl_printf(o, " down=%d delay_ms=%g loss_pct=%g rate_mbit=%g "
		   "retry_ms=%g age_s=%.1f drops=%" PRIu64 " lost=%" PRIu64
		   " fails=%" PRIu64 " devs=",
		   f->down, f->delay_ns / 1e6, f->loss_ppm / 1e4,
		   f->rate * 8 / 1e6, f->retry_ns / 1e6,
		   (now_ns() - f->since) / 1e9, RLX(&f->drops), RLX(&f->lost),
		   RLX(&f->fails));
	bool any = false;

	for (struct vdev *d = g_devs; d; d = d->next) {
		if (d->dead || !fault_matches(f, d))
			continue;
		ctl_printf(o, "%s%d:%s", any ? "," : "", d->id,
			   d->kind == KIND_NET ? "net" : "rdma");
		any = true;
	}
	ctl_printf(o, "%s\n", any ? "" : "-");
}

/* whether f and n name the same link: an IP, a MAC or a device */
static bool fault_same_link(const struct fault *f, const struct fault *n)
{
	if ((f->has_ip && n->has_ip && !memcmp(f->gid, n->gid, 16)) ||
	    (f->has_mac && n->has_mac && f->mac == n->mac))
		return true;
	for (struct vdev *d = g_devs; d; d = d->next)
		if (!d->dead && fault_matches(f, d) && fault_matches(n, d))
			return true;
	return false;
}

/*
 * One request: "set SEL... [down] [delay=MS] [loss=PCT] [rate=MBIT]
 * [retry=MS]", "clear SEL...|all" or "list", SEL being ip=ADDR and
 * mac=MAC.  The reply ends with "ok" or "error: why".
 */
static void ctl_request(char *line, struct ctl_out *o)
{
	char *save = NULL, *verb = strtok_r(line, " \t\r\n", &save), *tok;
	struct fault n = { 0 };
	bool all = false, any = false;
	double v;

	if (!verb) {
		ctl_printf(o, "error: empty request\n");
		return;
	}
	if (!strcmp(verb, "list")) {
		for (int i = 0; i < FAULT_MAX; i++)
			if (g_faults[i].used)
				fault_describe(o, &g_faults[i]);
		ctl_printf(o, "ok\n");
		return;
	}
	if (strcmp(verb, "set") && strcmp(verb, "clear")) {
		ctl_printf(o, "error: unknown request %s\n", verb);
		return;
	}
	while ((tok = strtok_r(NULL, " \t\r\n", &save))) {
		char *val = strchr(tok, '=');

		if (val)
			*val++ = 0;
		if (!strcmp(tok, "ip") && val) {
			if (!parse_ip(val, n.gid))
				goto bad;
			n.has_ip = true;
		} else if (!strcmp(tok, "mac") && val) {
			uint8_t m[6];

			if (!parse_mac(val, m))
				goto bad;
			n.mac = mac_key(m);
			ll_gid_of(m, n.ll_gid);
			n.has_mac = true;
		} else if (!strcmp(tok, "all") && !val && *verb == 'c') {
			all = true;
		} else if (*verb == 'c') {
			goto bad;
		} else if (!strcmp(tok, "down") && !val) {
			n.down = true;
		} else if (!strcmp(tok, "delay") && val &&
			   parse_num(val, 0, 3600e3, &v)) {
			n.delay_ns = v * 1e6;
		} else if (!strcmp(tok, "loss") && val &&
			   parse_num(val, 0, 100, &v)) {
			n.loss_ppm = v * 1e4;
		} else if (!strcmp(tok, "rate") && val &&
			   parse_num(val, 0, 1e7, &v)) {
			n.rate = v * 1e6 / 8;
			if (v && !n.rate)
				n.rate = 1;
		} else if (!strcmp(tok, "retry") && val &&
			   parse_num(val, 0, 3600e3, &v)) {
			n.retry_ns = v * 1e6;
		} else {
			goto bad;
		}
	}
	if (!all && !n.has_ip && !n.has_mac) {
		ctl_printf(o, "error: name the link with ip= or mac=\n");
		return;
	}

	wlock();
	for (int i = 0; i < FAULT_MAX; i++) {
		struct fault *f = &g_faults[i];

		if (!f->used || (!all && !fault_same_link(f, &n)))
			continue;
		f->used = false;
		g_nfaults--;
		any = true;
	}
	if (*verb == 's') {
		struct fault *f = NULL;

		for (int i = 0; i < FAULT_MAX && !f; i++)
			if (!g_faults[i].used)
				f = &g_faults[i];
		if (!f) {
			FAULT_GEN_BUMP();
			wunlock();
			ctl_printf(o, "error: %d links have faults already\n",
				   FAULT_MAX);
			return;
		}
		n.used = true;
		n.since = now_ns();
		*f = n;
		plat_spin_init(&f->tx.lock);
		plat_spin_init(&f->rx.lock);
		g_nfaults++;
		any = true;
		fault_describe(o, f);
	}
	FAULT_GEN_BUMP();
	wunlock();
	/* stalled QPs and queues look again */
	wake_workers();
	if (*verb == 'c' && !any && !all)
		ctl_printf(o, "error: no such link\n");
	else
		ctl_printf(o, "ok\n");
	{
		char d[sizeof(o->buf)];

		snprintf(d, sizeof(d), "%.*s", (int)o->len, o->buf);
		for (char *p = d; *p; p++)
			if (*p == '\n')
				*p = ' ';
		LOG("ctl: %s: %s", verb, d);
	}
	return;
bad:
	ctl_printf(o, "error: bad argument %s\n", tok);
}

/* a control connection: one request, one reply */
static void ctl_accept(void)
{
	struct ctl_out out;
	char line[1024];
	size_t got = 0;
	int s = plat_accept(g_lsock_ctl);

	if (s < 0)
		return;
	{
		struct pollfd pfd = { .fd = s, .events = POLLIN };
		/* the main thread serves every VM: one second for a request */
		uint64_t end = now_ns() + 1000000000ull;
		uint64_t t;

		while (got < sizeof(line) - 1 && (t = now_ns()) < end &&
		       poll(&pfd, 1, (end - t) / 1000000 + 1) == 1) {
			ssize_t r = read(s, line + got, sizeof(line) - 1 - got);

			if (r <= 0)
				break;
			got += r;
			if (memchr(line, '\n', got))
				break;
		}
	}
	line[got] = 0;
	out.len = 0;
	if (!memchr(line, '\n', got))
		ctl_printf(&out, "error: no complete request line\n");
	else
		ctl_request(line, &out);
	for (size_t off = 0; off < out.len;) {
		ssize_t w = write(s, out.buf + off, out.len - off);

		if (w <= 0)
			break;
		off += w;
	}
	close(s);
}
