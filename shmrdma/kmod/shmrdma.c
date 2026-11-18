// SPDX-License-Identifier: GPL-2.0
/*
 * shmrdma: a RoCE-like RDMA device for VMs on one host.
 *
 * The device is a vhost-user virtio device served by shmrdmad on the
 * host, which maps the RAM of every VM.  Work queues and completion
 * queues live in guest memory; the host executes RDMA operations as a
 * single memcpy from one guest's pages to another's.
 *
 * Connection management is ordinary RoCEv2: the device is bound to a
 * netdev, the core fills the GID table from its IP addresses, and
 * rdma_cm talks to the peer with MADs on QP1, which the host delivers
 * like any other UD send.
 */
#include <linux/module.h>
#include <linux/virtio.h>
#include <linux/virtio_config.h>
#include <linux/scatterlist.h>
#include <linux/vmalloc.h>
#include <linux/netdevice.h>
#include <linux/etherdevice.h>
#include <linux/idr.h>
#include <linux/xarray.h>
#include <linux/virtio_fs.h>
#include <linux/dma-mapping.h>
#include <net/addrconf.h>
#include <rdma/ib_verbs.h>
#include <rdma/ib_mad.h>

#include "../include/shmrdma_abi.h"

static char *netdev = "eth0";
module_param(netdev, charp, 0444);
MODULE_PARM_DESC(netdev, "netdev whose IP addresses give the GIDs, for a device tagged plain shmrdma (shmrdma@IFNAME names its own)");

#define SHMRDMA_EQ_ENTRIES	16384
#define SHMRDMA_EQ_BUDGET	1024	/* EQEs per pass with interrupts off */
#define SHMRDMA_NEVBUF		8
#define SHMRDMA_CQE_SIZE	32
#define SHMRDMA_QPN_NONE	U32_MAX	/* a cleaned CQE: no QP has it */
#define SHMRDMA_MAX_PD		(1 << 20)

#ifndef HAVE_VIRTIO_RESET_DEVICE
static inline void virtio_reset_device(struct virtio_device *vdev)
{
	vdev->config->reset(vdev);
}
#endif

struct shmrdma_dev {
	struct ib_device ibdev;
	struct virtio_device *vdev;
	struct virtqueue *ctrl_vq;
	struct virtqueue *event_vq;
	struct virtqueue *db_vq;
	/* NULL once the netdev unregisters; dropped then, not held */
	struct net_device __rcu *ndev;
	struct notifier_block nb;
	bool nb_registered;
	/* the port state last reported, under rtnl */
	bool port_up;
	char ifname[IFNAMSIZ];	/* the netdev it is bound to */

	struct mutex ctrl_mutex;
	struct completion ctrl_done;
	struct shmrdma_cmd *cmdbuf;
	struct shmrdma_resp *respbuf;
	bool broken;
	/* the device was reset: the backend holds nothing of ours */
	bool reset_done;
	bool fatal_sent;
	struct work_struct fatal_work;
	/* work posted after the reset: have its CQs polled for the flush */
	struct work_struct flush_work;
	/* EQEs left over when an interrupt ran out of budget */
	struct work_struct eq_work;

	spinlock_t eq_lock;
	struct shmrdma_dev_shared *shared;
	struct shmrdma_eqe *eq;
	u32 eq_cons;
	u64 *evbufs;

	struct xarray qps;
	struct xarray cqs;
	struct ida qpn_ida;
	u32 next_qpn;
	struct ida cqn_ida;
	u32 next_cqn;
	struct ida mr_ida;
	u32 next_mrn;
	atomic_t mr_variant;
	struct ida pdn_ida;

	struct ib_device_attr attr;
	bool registered;
};

struct shmrdma_pd {
	struct ib_pd ibpd;
	u32 pdn;
};

struct shmrdma_ah {
	struct ib_ah ibah;
	u8 dgid[16];
	u8 sgid[16];
};

struct shmrdma_ring {
	void *buf;
	u64 *pages;
	u32 npages;
	u32 entries;
	u32 esz;
};

struct shmrdma_cq {
	struct ib_cq ibcq;
	u32 cqn;
	spinlock_t lock;
	struct shmrdma_cq_rec *rec;
	struct shmrdma_ring ring;
	u32 cons;
	bool ring_bad;		/* the backend's producer ran past the ring */
	bool err_seen;		/* its CQ error was reported, under eq_lock */
	struct work_struct ev_work;
	/* QPs sending and receiving on this CQ, under lock */
	struct list_head sq_qps, rq_qps;
};

struct shmrdma_qp {
	struct ib_qp ibqp;
	struct shmrdma_cq *scq, *rcq;
	struct list_head scq_node, rcq_node;
	u32 type;
	struct shmrdma_qp_rec *rec;
	struct shmrdma_ring sq, rq;
	spinlock_t sq_lock, rq_lock;
	u32 sq_prod, rq_prod;
	u32 max_send_sge, max_recv_sge, max_inline;
	bool sig_all;
	bool fatal_seen;	/* its QP_FATAL was reported, under eq_lock */
	struct work_struct ev_work;
	struct mutex state_lock;
	struct ib_qp_attr attr;
	u8 dgid[16];
};

struct shmrdma_mr {
	struct ib_mr ibmr;
	u32 index;
	u64 *pages;
	u32 npages;
	u32 max_pages;
	u32 fpo;
};

static inline struct shmrdma_dev *to_sdev(struct ib_device *d)
{
	return container_of(d, struct shmrdma_dev, ibdev);
}

static inline struct shmrdma_pd *to_spd(struct ib_pd *p)
{
	return container_of(p, struct shmrdma_pd, ibpd);
}

static inline struct shmrdma_cq *to_scq(struct ib_cq *c)
{
	return container_of(c, struct shmrdma_cq, ibcq);
}

static inline struct shmrdma_qp *to_sqp(struct ib_qp *q)
{
	return container_of(q, struct shmrdma_qp, ibqp);
}

static inline struct shmrdma_mr *to_smr(struct ib_mr *m)
{
	return container_of(m, struct shmrdma_mr, ibmr);
}

static inline struct shmrdma_ah *to_sah(struct ib_ah *a)
{
	return container_of(a, struct shmrdma_ah, ibah);
}

/* ---------------------------------------------------------------- */
/* control path                                                     */

static void shmrdma_ctrl_cb(struct virtqueue *vq)
{
	struct shmrdma_dev *dev = vq->vdev->priv;

	complete(&dev->ctrl_done);
}

/*
 * Stop the backend using this guest's memory: QEMU stops its vrings and
 * waits for its reply, which it sends only once it has stopped using
 * the device's QPs, CQs and MRs, and then forwards the reset, on which
 * it drops them.  Memory it was given may be freed after this.  Caller
 * holds ctrl_mutex.
 */
static void shmrdma_reset_locked(struct shmrdma_dev *dev)
{
	dev->broken = true;
	if (dev->reset_done)
		return;
	virtio_break_device(dev->vdev);
	virtio_reset_device(dev->vdev);
	smp_store_release(&dev->reset_done, true);
}

/* an event nobody asked for; a polled-only CQ WARNs on one */
static void shmrdma_cq_comp(struct shmrdma_cq *cq)
{
	if (cq->ibcq.comp_handler && cq->ibcq.poll_ctx != IB_POLL_DIRECT)
		cq->ibcq.comp_handler(&cq->ibcq, cq->ibcq.cq_context);
}

static bool shmrdma_cq_pending(struct shmrdma_dev *dev, struct shmrdma_cq *cq);

/*
 * After the reset: the event an armed CQ with something to report waits
 * for, so its ULP polls the flushes.  With all, armed or not: the
 * backend may have consumed an arm for an event the broken event queue
 * never delivered.  A CQ with nothing to report, one ib_free_cq is
 * freeing among them, is left alone.
 */
static void shmrdma_kick_cqs(struct shmrdma_dev *dev, bool all)
{
	struct shmrdma_cq *cq;
	unsigned long flags, i;

	spin_lock_irqsave(&dev->eq_lock, flags);
	xa_for_each(&dev->cqs, i, cq)
		if (shmrdma_cq_pending(dev, cq) &&
		    (xchg(&cq->rec->arm, 0) || all))
			shmrdma_cq_comp(cq);
	spin_unlock_irqrestore(&dev->eq_lock, flags);
}

static void shmrdma_flush_work(struct work_struct *work)
{
	shmrdma_kick_cqs(container_of(work, struct shmrdma_dev, flush_work),
			 false);
}

static void shmrdma_fatal_work(struct work_struct *work)
{
	struct shmrdma_dev *dev = container_of(work, struct shmrdma_dev,
					       fatal_work);
	struct ib_event ev = {
		.device = &dev->ibdev,
		.event = IB_EVENT_DEVICE_FATAL,
		.element.port_num = 1,
	};
	bool send;

	mutex_lock(&dev->ctrl_mutex);
	shmrdma_reset_locked(dev);
	send = !dev->fatal_sent && dev->registered;
	if (send)
		dev->fatal_sent = true;
	mutex_unlock(&dev->ctrl_mutex);
	if (send)
		ib_dispatch_event(&ev);
	shmrdma_kick_cqs(dev, true);
}

/* a command was not carried out: reset now, report it from a work item */
static void shmrdma_fatal(struct shmrdma_dev *dev)
{
	mutex_lock(&dev->ctrl_mutex);
	shmrdma_reset_locked(dev);
	mutex_unlock(&dev->ctrl_mutex);
	schedule_work(&dev->fatal_work);
}

static int shmrdma_cmd(struct shmrdma_dev *dev, struct shmrdma_cmd *cmd,
		       struct shmrdma_resp *resp)
{
	struct scatterlist out, in, *sgs[2];
	unsigned int len;
	int rc;

	mutex_lock(&dev->ctrl_mutex);
	if (dev->broken) {
		rc = -EIO;
		goto out;
	}
	memcpy(dev->cmdbuf, cmd, sizeof(*cmd));
	memset(dev->respbuf, 0, sizeof(*dev->respbuf));
	dev->respbuf->status = -EIO;
	sg_init_one(&out, dev->cmdbuf, sizeof(*cmd));
	sg_init_one(&in, dev->respbuf, sizeof(*dev->respbuf));
	sgs[0] = &out;
	sgs[1] = &in;
	reinit_completion(&dev->ctrl_done);
	rc = virtqueue_add_sgs(dev->ctrl_vq, sgs, 1, 1, dev, GFP_KERNEL);
	if (rc)
		goto out;
	virtqueue_kick(dev->ctrl_vq);
	for (;;) {
		if (!wait_for_completion_timeout(&dev->ctrl_done, 30 * HZ)) {
			dev_err(&dev->vdev->dev,
				"command %u timed out, resetting the device\n",
				cmd->cmd);
			shmrdma_reset_locked(dev);
			schedule_work(&dev->fatal_work);
			rc = -ETIMEDOUT;
			goto out;
		}
		if (virtqueue_get_buf(dev->ctrl_vq, &len))
			break;
	}
	rc = dev->respbuf->status;
	if (rc > 0 || rc < -MAX_ERRNO)
		rc = -EIO;
	if (resp)
		*resp = *dev->respbuf;
out:
	mutex_unlock(&dev->ctrl_mutex);
	return rc;
}

/*
 * A destroy the backend did not carry out leaves it using the object's
 * memory, so the device is reset before that memory is freed.
 */
static void shmrdma_destroy_failed(struct shmrdma_dev *dev, const char *what,
				   u32 n, int rc)
{
	if (READ_ONCE(dev->reset_done))
		return;
	dev_err(&dev->vdev->dev, "%s %u failed (%d), resetting the device\n",
		what, n, rc);
	shmrdma_fatal(dev);
}

/* ---------------------------------------------------------------- */
/* rings                                                            */

static int ring_alloc(struct shmrdma_ring *r, u32 entries, u32 esz)
{
	size_t bytes = PAGE_ALIGN((size_t)entries * esz);
	u32 i;

	r->entries = entries;
	r->esz = esz;
	/* the ABI's 4 KB ring pages the entries span, several per page on
	 * 16K/64K kernels */
	r->npages = DIV_ROUND_UP((size_t)entries * esz, SHMRDMA_RING_PAGE_SIZE);
	r->buf = vzalloc(bytes);
	if (!r->buf)
		return -ENOMEM;
	r->pages = kmalloc_array(r->npages, sizeof(u64), GFP_KERNEL);
	if (!r->pages) {
		vfree(r->buf);
		r->buf = NULL;
		return -ENOMEM;
	}
	for (i = 0; i < r->npages; i++) {
		size_t off = (size_t)i << SHMRDMA_RING_PAGE_SHIFT;

		r->pages[i] = page_to_phys(vmalloc_to_page(r->buf + off)) +
			      (off & ~PAGE_MASK);
	}
	return 0;
}

static void ring_free(struct shmrdma_ring *r)
{
	vfree(r->buf);
	kfree(r->pages);
	r->buf = NULL;
	r->pages = NULL;
}

static void ring_desc(struct shmrdma_ring *r, struct shmrdma_ring_desc *d)
{
	d->pages_gpa = virt_to_phys(r->pages);
	d->npages = r->npages;
	d->entries = r->entries;
	d->entry_size = r->esz;
}

static inline void *ring_at(struct shmrdma_ring *r, u32 idx)
{
	return r->buf + (size_t)(idx & (r->entries - 1)) * r->esz;
}

/* ---------------------------------------------------------------- */
/* events                                                           */

static bool shmrdma_cq_has_qps(struct shmrdma_cq *cq)
{
	unsigned long flags;
	bool any;

	spin_lock_irqsave(&cq->lock, flags);
	any = !list_empty(&cq->sq_qps) || !list_empty(&cq->rq_qps);
	spin_unlock_irqrestore(&cq->lock, flags);
	return any;
}

/* handlers run in process context; destroy cancels the work */
static void shmrdma_qp_ev_work(struct work_struct *work)
{
	struct shmrdma_qp *qp = container_of(work, struct shmrdma_qp, ev_work);
	struct ib_event ev = {
		.device = qp->ibqp.device,
		.event = IB_EVENT_QP_FATAL,
		.element.qp = &qp->ibqp,
	};

	qp->ibqp.event_handler(&ev, qp->ibqp.qp_context);
}

static void shmrdma_cq_ev_work(struct work_struct *work)
{
	struct shmrdma_cq *cq = container_of(work, struct shmrdma_cq, ev_work);
	struct ib_event ev = {
		.device = cq->ibcq.device,
		.event = IB_EVENT_CQ_ERR,
		.element.cq = &cq->ibcq,
	};

	cq->ibcq.event_handler(&ev, cq->ibcq.cq_context);
}

/* caller holds eq_lock, which destroy_cq and destroy_qp take to erase */
static void shmrdma_qp_fatal(struct shmrdma_dev *dev, struct shmrdma_qp *qp)
{
	if (qp->fatal_seen)
		return;
	qp->fatal_seen = true;
	WRITE_ONCE(qp->attr.qp_state, IB_QPS_ERR);
	if (qp->ibqp.event_handler)
		schedule_work(&qp->ev_work);
}

static void shmrdma_cq_err(struct shmrdma_dev *dev, struct shmrdma_cq *cq)
{
	if (cq->err_seen)
		return;
	cq->err_seen = true;
	dev_err(&dev->vdev->dev, "cq %u overflowed\n", cq->cqn);
	if (cq->ibcq.event_handler)
		schedule_work(&cq->ev_work);
}

/* caller holds eq_lock */
static void shmrdma_eqe(struct shmrdma_dev *dev, u32 n, u32 type)
{
	struct shmrdma_qp *qp;
	struct shmrdma_cq *cq;

	switch (type) {
	case SHMRDMA_EQE_QP_FATAL:
		qp = xa_load(&dev->qps, n);
		/* an event raised before a RESET the daemon has since done */
		if (qp && READ_ONCE(qp->rec->fatal))
			shmrdma_qp_fatal(dev, qp);
		return;
	case SHMRDMA_EQE_CQ_ERR:
		cq = xa_load(&dev->cqs, n);
		if (cq)
			shmrdma_cq_err(dev, cq);
		return;
	}
	/* not to a CQ ib_free_cq may be freeing, or one not set up yet: the
	 * QPs leave it under eq_lock, before it is freed */
	cq = xa_load(&dev->cqs, n);
	if (cq && shmrdma_cq_has_qps(cq))
		shmrdma_cq_comp(cq);
}

/*
 * Handle up to SHMRDMA_EQ_BUDGET EQEs and rearm the EQ.  Returns true
 * when the budget ran out first: the EQ is then left unarmed, so no
 * interrupt comes for the rest, and eq_work must be scheduled.  Caller
 * holds eq_lock.
 */
static bool shmrdma_eq_process(struct shmrdma_dev *dev)
{
	struct shmrdma_dev_shared *sh = dev->shared;
	int budget = SHMRDMA_EQ_BUDGET;

	for (;;) {
		u32 prod = smp_load_acquire(&sh->eq_prod);
		bool lost = READ_ONCE(sh->eq_lost) && xchg(&sh->eq_lost, 0);

		if (prod - dev->eq_cons > SHMRDMA_EQ_ENTRIES) {
			/* events were overwritten */
			dev_warn_ratelimited(&dev->vdev->dev,
					     "EQ producer %u is %u past %u\n",
					     prod, prod - dev->eq_cons,
					     dev->eq_cons);
			dev->eq_cons = prod;
			smp_store_release(&sh->eq_cons, dev->eq_cons);
			lost = true;
		}
		if (lost) {
			struct shmrdma_cq *cq;
			struct shmrdma_qp *qp;
			unsigned long i;

			/* any CQ or QP may have an event that never came */
			xa_for_each(&dev->qps, i, qp)
				if (READ_ONCE(qp->rec->fatal))
					shmrdma_qp_fatal(dev, qp);
			xa_for_each(&dev->cqs, i, cq) {
				if (READ_ONCE(cq->rec->err))
					shmrdma_cq_err(dev, cq);
				if (shmrdma_cq_pending(dev, cq))
					shmrdma_cq_comp(cq);
			}
		}
		while (dev->eq_cons != prod) {
			struct shmrdma_eqe *e =
				&dev->eq[dev->eq_cons & (SHMRDMA_EQ_ENTRIES - 1)];
			u32 cqn = READ_ONCE(e->cqn);
			u32 type = READ_ONCE(e->type);

			if (!budget--)
				return true;
			dev->eq_cons++;
			/* the backend writes the slot once it reads this */
			smp_store_release(&sh->eq_cons, dev->eq_cons);
			shmrdma_eqe(dev, cqn, type);
		}
		WRITE_ONCE(sh->eq_armed, 1);
		smp_mb();
		if (dev->eq_cons == READ_ONCE(sh->eq_prod) &&
		    !READ_ONCE(sh->eq_lost))
			return false;
	}
}

static void shmrdma_eq_work(struct work_struct *work)
{
	struct shmrdma_dev *dev = container_of(work, struct shmrdma_dev,
					       eq_work);
	unsigned long flags;
	bool more;

	spin_lock_irqsave(&dev->eq_lock, flags);
	more = shmrdma_eq_process(dev);
	spin_unlock_irqrestore(&dev->eq_lock, flags);
	if (more)
		schedule_work(&dev->eq_work);
}

static void shmrdma_event_cb(struct virtqueue *vq)
{
	struct shmrdma_dev *dev = vq->vdev->priv;
	unsigned long flags;
	unsigned int len;
	struct scatterlist sg;
	void *buf;
	bool kick = false, more = false;

	spin_lock_irqsave(&dev->eq_lock, flags);
	while ((buf = virtqueue_get_buf(vq, &len)) != NULL) {
		sg_init_one(&sg, buf, sizeof(u64));
		virtqueue_add_inbuf(vq, &sg, 1, buf, GFP_ATOMIC);
		kick = true;
	}
	if (kick)
		virtqueue_kick(vq);
	if (dev->shared)
		more = shmrdma_eq_process(dev);
	spin_unlock_irqrestore(&dev->eq_lock, flags);
	if (more)
		schedule_work(&dev->eq_work);
}

static inline void shmrdma_doorbell(struct shmrdma_dev *dev,
				    struct shmrdma_qp_rec *rec)
{
	smp_mb();
	if (READ_ONCE(rec->need_kick))
		virtqueue_notify(dev->db_vq);
}

/* ---------------------------------------------------------------- */
/* device and port                                                  */

static int shmrdma_query_device(struct ib_device *ibdev,
				struct ib_device_attr *attr,
				struct ib_udata *udata)
{
	*attr = to_sdev(ibdev)->attr;
	return 0;
}

static bool shmrdma_port_up(struct shmrdma_dev *dev)
{
	struct net_device *ndev;
	bool up;

	rcu_read_lock();
	ndev = rcu_dereference(dev->ndev);
	up = ndev && netif_running(ndev) && netif_carrier_ok(ndev) &&
	     !READ_ONCE(dev->broken);
	rcu_read_unlock();
	return up;
}

static int shmrdma_query_port(struct ib_device *ibdev, u32 port,
			      struct ib_port_attr *attr)
{
	struct shmrdma_dev *dev = to_sdev(ibdev);
	bool up;

	if (port != 1)
		return -EINVAL;
	up = shmrdma_port_up(dev);
	attr->state = up ? IB_PORT_ACTIVE : IB_PORT_DOWN;
	attr->phys_state = up ? IB_PORT_PHYS_STATE_LINK_UP :
				IB_PORT_PHYS_STATE_DISABLED;
	attr->max_mtu = IB_MTU_4096;
	attr->active_mtu = IB_MTU_4096;
	attr->gid_tbl_len = SHMRDMA_MAX_GID;
	attr->port_cap_flags = IB_PORT_CM_SUP;
	attr->max_msg_sz = SHMRDMA_MAX_MSG;
	attr->pkey_tbl_len = 1;
	attr->active_width = IB_WIDTH_4X;
	attr->active_speed = IB_SPEED_NDR;
	attr->max_vl_num = 1;
	return 0;
}

static int shmrdma_get_port_immutable(struct ib_device *ibdev, u32 port,
				      struct ib_port_immutable *im)
{
	if (port != 1)
		return -EINVAL;
	im->core_cap_flags = RDMA_CORE_PORT_IBA_ROCE_UDP_ENCAP;
	im->pkey_tbl_len = 1;
	im->gid_tbl_len = SHMRDMA_MAX_GID;
	im->max_mad_size = IB_MGMT_MAD_SIZE;
	return 0;
}

static enum rdma_link_layer shmrdma_link_layer(struct ib_device *ibdev,
					       u32 port)
{
	return IB_LINK_LAYER_ETHERNET;
}

static int shmrdma_query_pkey(struct ib_device *ibdev, u32 port, u16 index,
			      u16 *pkey)
{
	if (index != 0)
		return -EINVAL;
	*pkey = IB_DEFAULT_PKEY_FULL;
	return 0;
}

static int shmrdma_add_gid(const struct ib_gid_attr *attr, void **context)
{
	struct shmrdma_dev *dev = to_sdev(attr->device);
	struct shmrdma_cmd cmd = { .cmd = SHMRDMA_CMD_ADD_GID };

	cmd.gid.index = attr->index;
	memcpy(cmd.gid.gid, attr->gid.raw, 16);
	return shmrdma_cmd(dev, &cmd, NULL);
}

static int shmrdma_del_gid(const struct ib_gid_attr *attr, void **context)
{
	struct shmrdma_dev *dev = to_sdev(attr->device);
	struct shmrdma_cmd cmd = { .cmd = SHMRDMA_CMD_DEL_GID };

	cmd.gid.index = attr->index;
	memcpy(cmd.gid.gid, attr->gid.raw, 16);
	return shmrdma_cmd(dev, &cmd, NULL);
}

static int shmrdma_alloc_pd(struct ib_pd *pd, struct ib_udata *udata)
{
	struct shmrdma_dev *dev = to_sdev(pd->device);
	int rc;

	if (udata)
		return -EOPNOTSUPP;
	rc = ida_alloc_range(&dev->pdn_ida, 1, SHMRDMA_MAX_PD - 1, GFP_KERNEL);
	if (rc < 0)
		return rc;
	to_spd(pd)->pdn = rc;
	return 0;
}

static int shmrdma_dealloc_pd(struct ib_pd *pd, struct ib_udata *udata)
{
	ida_free(&to_sdev(pd->device)->pdn_ida, to_spd(pd)->pdn);
	return 0;
}

/* ---------------------------------------------------------------- */
/* address handles                                                  */

static int shmrdma_create_ah(struct ib_ah *ibah,
			     struct rdma_ah_init_attr *init_attr,
			     struct ib_udata *udata)
{
	struct shmrdma_ah *ah = to_sah(ibah);
	const struct ib_global_route *grh;

	if (!(rdma_ah_get_ah_flags(init_attr->ah_attr) & IB_AH_GRH))
		return -EINVAL;
	grh = rdma_ah_read_grh(init_attr->ah_attr);
	memcpy(ah->dgid, grh->dgid.raw, 16);
	if (grh->sgid_attr)
		memcpy(ah->sgid, grh->sgid_attr->gid.raw, 16);
	return 0;
}

static int shmrdma_destroy_ah(struct ib_ah *ibah, u32 flags)
{
	return 0;
}

/*
 * Round robin from *next, not lowest free.  A QPN freed just now is
 * still in the peer CM's timewait, which rejects a reconnect that reuses
 * it; a CQN freed just now may still have an EQE on its way; an MR index
 * freed just now may still be in a peer's rkey.
 */
static int ida_alloc_rr(struct ida *ida, u32 *next, u32 min, u32 max)
{
	u32 start = READ_ONCE(*next);
	int rc;

	if (start < min || start > max)
		start = min;
	rc = ida_alloc_range(ida, start, max, GFP_KERNEL);
	if (rc == -ENOSPC && start > min)
		rc = ida_alloc_range(ida, min, max, GFP_KERNEL);
	if (rc >= 0)
		WRITE_ONCE(*next, rc + 1);
	return rc;
}

/* ---------------------------------------------------------------- */
/* completion queues                                                */

static int shmrdma_create_cq(struct ib_cq *ibcq,
			     const struct ib_cq_init_attr *attr,
#ifdef HAVE_CREATE_CQ_ATTRS
			     struct uverbs_attr_bundle *attrs)
#else
			     struct ib_udata *udata)
#endif
{
	struct shmrdma_dev *dev = to_sdev(ibcq->device);
	struct shmrdma_cq *cq = to_scq(ibcq);
	struct shmrdma_cmd cmd = { .cmd = SHMRDMA_CMD_CREATE_CQ };
	unsigned long flags;
	u32 entries;
	int rc;

	if (attr->flags || attr->cqe < 1 || attr->cqe >= SHMRDMA_MAX_CQE)
		return -EINVAL;
	entries = roundup_pow_of_two(attr->cqe + 1);
	rc = ida_alloc_rr(&dev->cqn_ida, &dev->next_cqn, 0, SHMRDMA_MAX_CQ - 1);
	if (rc < 0)
		return rc;
	cq->cqn = rc;
	spin_lock_init(&cq->lock);
	INIT_LIST_HEAD(&cq->sq_qps);
	INIT_LIST_HEAD(&cq->rq_qps);
	INIT_WORK(&cq->ev_work, shmrdma_cq_ev_work);
	cq->rec = kzalloc(sizeof(*cq->rec), GFP_KERNEL);
	if (!cq->rec) {
		rc = -ENOMEM;
		goto err_ida;
	}
	rc = ring_alloc(&cq->ring, entries, SHMRDMA_CQE_SIZE);
	if (rc)
		goto err_rec;
	cmd.create_cq.cqn = cq->cqn;
	cmd.create_cq.rec_gpa = virt_to_phys(cq->rec);
	ring_desc(&cq->ring, &cmd.create_cq.ring);
	rc = xa_err(xa_store_irq(&dev->cqs, cq->cqn, cq, GFP_KERNEL));
	if (rc)
		goto err_ring;
	rc = shmrdma_cmd(dev, &cmd, NULL);
	if (rc)
		goto err_xa;
	ibcq->cqe = entries - 1;
	return 0;

err_xa:
	spin_lock_irqsave(&dev->eq_lock, flags);
	xa_erase(&dev->cqs, cq->cqn);
	spin_unlock_irqrestore(&dev->eq_lock, flags);
	cancel_work_sync(&cq->ev_work);
err_ring:
	ring_free(&cq->ring);
err_rec:
	kfree(cq->rec);
err_ida:
	ida_free(&dev->cqn_ida, cq->cqn);
	return rc;
}

static int shmrdma_destroy_cq(struct ib_cq *ibcq, struct ib_udata *udata)
{
	struct shmrdma_dev *dev = to_sdev(ibcq->device);
	struct shmrdma_cq *cq = to_scq(ibcq);
	struct shmrdma_cmd cmd = { .cmd = SHMRDMA_CMD_DESTROY_CQ };
	unsigned long flags;
	int rc;

	cmd.destroy_cq.cqn = cq->cqn;
	rc = shmrdma_cmd(dev, &cmd, NULL);
	if (rc)
		shmrdma_destroy_failed(dev, "destroy cq", cq->cqn, rc);
	/* no completion or event handler may run after this */
	spin_lock_irqsave(&dev->eq_lock, flags);
	xa_erase(&dev->cqs, cq->cqn);
	spin_unlock_irqrestore(&dev->eq_lock, flags);
	cancel_work_sync(&cq->ev_work);
	ring_free(&cq->ring);
	kfree(cq->rec);
	ida_free(&dev->cqn_ida, cq->cqn);
	return 0;
}

static const enum ib_wc_opcode wc_opcode[] = {
	[SHMRDMA_WC_SEND] = IB_WC_SEND,
	[SHMRDMA_WC_RDMA_WRITE] = IB_WC_RDMA_WRITE,
	[SHMRDMA_WC_RDMA_READ] = IB_WC_RDMA_READ,
	[SHMRDMA_WC_REG_MR] = IB_WC_REG_MR,
	[SHMRDMA_WC_LOCAL_INV] = IB_WC_LOCAL_INV,
	[SHMRDMA_WC_RECV] = IB_WC_RECV,
	[SHMRDMA_WC_RECV_RDMA_IMM] = IB_WC_RECV_RDMA_WITH_IMM,
};

static const enum ib_wc_opcode sq_wc_opcode[] = {
	[SHMRDMA_OP_SEND] = IB_WC_SEND,
	[SHMRDMA_OP_SEND_IMM] = IB_WC_SEND,
	[SHMRDMA_OP_SEND_INV] = IB_WC_SEND,
	[SHMRDMA_OP_RDMA_WRITE] = IB_WC_RDMA_WRITE,
	[SHMRDMA_OP_RDMA_WRITE_IMM] = IB_WC_RDMA_WRITE,
	[SHMRDMA_OP_RDMA_READ] = IB_WC_RDMA_READ,
	[SHMRDMA_OP_REG_MR] = IB_WC_REG_MR,
	[SHMRDMA_OP_LOCAL_INV] = IB_WC_LOCAL_INV,
};

static void flush_wc(struct ib_wc *w, struct shmrdma_qp *qp, u64 wr_id,
		     enum ib_wc_opcode op)
{
	memset(w, 0, sizeof(*w));
	w->wr_id = wr_id;
	w->status = IB_WC_WR_FLUSH_ERR;
	w->opcode = op;
	w->qp = &qp->ibqp;
	w->port_num = 1;
}

/*
 * The WR a dead backend freed the slot of without making its CQE
 * visible, which only the QP record still describes; cons is the
 * queue's consumer index.  With wc NULL, only say whether there is one.
 */
static int shmrdma_flush_limbo(struct shmrdma_dev *dev, struct shmrdma_cq *cq,
			       struct shmrdma_qp *qp, bool sq, u32 cons,
			       struct ib_wc *wc)
{
	struct shmrdma_qp_rec *r = qp->rec;
	u32 *done = sq ? &r->sq_done : &r->rq_done;
	u32 *op = sq ? &r->sq_limbo_op : &r->rq_limbo_op;
	u32 *pos = sq ? &r->sq_limbo_pos : &r->rq_limbo_pos;
	u64 *wr_id = sq ? &r->sq_limbo_wr_id : &r->rq_limbo_wr_id;
	u32 o = READ_ONCE(*op);
	int owed;

	if (READ_ONCE(*done) == cons)
		return 0;
	owed = shmrdma_limbo_owed(READ_ONCE(*done), cons, o, READ_ONCE(*pos),
				  READ_ONCE(cq->rec->prod));
	if (!wc)
		return owed;
	if (owed)
		flush_wc(wc, qp, READ_ONCE(*wr_id),
			 o < ARRAY_SIZE(wc_opcode) ? wc_opcode[o] :
			 sq ? IB_WC_SEND : IB_WC_RECV);
	WRITE_ONCE(*done, cons);
	return owed;
}

/*
 * Once the device is reset the backend completes nothing more, so the
 * work still posted on cq's QPs is flushed here, from the consumer
 * indexes the backend left; with wc NULL, only say whether there is
 * any.  Caller holds cq->lock, under which destroy_qp unlinks the QP
 * before freeing it, and has polled every CQE the backend made visible.
 */
static int shmrdma_flush_cq(struct shmrdma_dev *dev, struct shmrdma_cq *cq,
			    int num, struct ib_wc *wc)
{
	struct shmrdma_qp *qp;
	int n = 0;
	u32 c;

	list_for_each_entry(qp, &cq->sq_qps, scq_node) {
		if (n == num)
			return n;
		spin_lock(&qp->sq_lock);
		c = READ_ONCE(qp->rec->sq_cons);
		n += shmrdma_flush_limbo(dev, cq, qp, true, c,
					 wc ? &wc[n] : NULL);
		if (!wc && n) {
			spin_unlock(&qp->sq_lock);
			return n;
		}
		if (qp->sq_prod - c > qp->sq.entries) {
			dev_err(&dev->vdev->dev, "qp %u sq consumer %u is %u behind %u\n",
				qp->ibqp.qp_num, c, qp->sq_prod - c, qp->sq_prod);
			c = qp->sq_prod;
		}
		if (!wc && c != qp->sq_prod)
			n = 1;
		for (; wc && n < num && c != qp->sq_prod; c++, n++) {
			struct shmrdma_sq_wqe *e = ring_at(&qp->sq, c);
			u32 op = READ_ONCE(e->opcode);

			flush_wc(&wc[n], qp, READ_ONCE(e->wr_id),
				 op < ARRAY_SIZE(sq_wc_opcode) ?
				 sq_wc_opcode[op] : IB_WC_SEND);
		}
		WRITE_ONCE(qp->rec->sq_cons, c);
		WRITE_ONCE(qp->rec->sq_done, c);
		spin_unlock(&qp->sq_lock);
	}
	list_for_each_entry(qp, &cq->rq_qps, rcq_node) {
		if (n == num)
			return n;
		spin_lock(&qp->rq_lock);
		c = READ_ONCE(qp->rec->rq_cons);
		n += shmrdma_flush_limbo(dev, cq, qp, false, c,
					 wc ? &wc[n] : NULL);
		if (!wc && n) {
			spin_unlock(&qp->rq_lock);
			return n;
		}
		if (qp->rq_prod - c > qp->rq.entries) {
			dev_err(&dev->vdev->dev, "qp %u rq consumer %u is %u behind %u\n",
				qp->ibqp.qp_num, c, qp->rq_prod - c, qp->rq_prod);
			c = qp->rq_prod;
		}
		if (!wc && c != qp->rq_prod)
			n = 1;
		for (; wc && n < num && c != qp->rq_prod; c++, n++) {
			struct shmrdma_rq_wqe *e = ring_at(&qp->rq, c);

			flush_wc(&wc[n], qp, e->wr_id, IB_WC_RECV);
		}
		WRITE_ONCE(qp->rec->rq_cons, c);
		WRITE_ONCE(qp->rec->rq_done, c);
		spin_unlock(&qp->rq_lock);
	}
	return n;
}

/* the ring's producer, or its consumer when the backend broke it */
static u32 shmrdma_cq_prod(struct shmrdma_dev *dev, struct shmrdma_cq *cq)
{
	u32 prod = smp_load_acquire(&cq->rec->prod);

	if (!cq->ring_bad && prod - cq->cons > cq->ring.entries) {
		dev_err(&dev->vdev->dev, "cq %u producer %u is %u past %u\n",
			cq->cqn, prod, prod - cq->cons, cq->cons);
		cq->ring_bad = true;
		schedule_work(&dev->fatal_work);
	}
	return cq->ring_bad ? cq->cons : prod;
}

/* CQEs to poll for a QP, or flushes once the device is reset */
static bool shmrdma_cq_pending(struct shmrdma_dev *dev, struct shmrdma_cq *cq)
{
	unsigned long flags;
	bool reset, pending;

	spin_lock_irqsave(&cq->lock, flags);
	reset = smp_load_acquire(&dev->reset_done);
	pending = (!list_empty(&cq->sq_qps) || !list_empty(&cq->rq_qps)) &&
		  (cq->cons != shmrdma_cq_prod(dev, cq) ||
		   (reset && shmrdma_flush_cq(dev, cq, 1, NULL)));
	spin_unlock_irqrestore(&cq->lock, flags);
	return pending;
}

static int shmrdma_poll_cq(struct ib_cq *ibcq, int num, struct ib_wc *wc)
{
	struct shmrdma_dev *dev = to_sdev(ibcq->device);
	struct shmrdma_cq *cq = to_scq(ibcq);
	unsigned long flags;
	bool reset;
	int n = 0;
	u32 prod;

	spin_lock_irqsave(&cq->lock, flags);
	/* before the producer: after the reset it moves no more */
	reset = smp_load_acquire(&dev->reset_done);
	prod = shmrdma_cq_prod(dev, cq);
	while (n < num && cq->cons != prod) {
		struct shmrdma_cqe c;
		struct shmrdma_qp *qp;
		struct ib_wc *w = &wc[n];

		/* the backend may write the slot: use only this copy */
		memcpy(&c, ring_at(&cq->ring, cq->cons), sizeof(c));
		barrier();
		cq->cons++;
		/* a QP of another CQ is freed once out of the xarray */
		xa_lock(&dev->qps);
		qp = xa_load(&dev->qps, c.qpn);
		if (qp && qp->scq != cq && qp->rcq != cq)
			qp = NULL;
		xa_unlock(&dev->qps);
		if (!qp)
			continue;
		memset(w, 0, sizeof(*w));
		w->wr_id = c.wr_id;
		w->status = c.status;
		w->opcode = c.opcode < ARRAY_SIZE(wc_opcode) ?
			    wc_opcode[c.opcode] : IB_WC_SEND;
		w->vendor_err = c.vendor_err;
		w->byte_len = min_t(u32, c.byte_len, SHMRDMA_MAX_MSG);
		w->qp = &qp->ibqp;
		w->src_qp = c.src_qp;
		w->port_num = 1;
		if (c.flags & SHMRDMA_CQE_IMM) {
			w->wc_flags |= IB_WC_WITH_IMM;
			w->ex.imm_data = (__force __be32)c.ex;
		}
		if (c.flags & SHMRDMA_CQE_INV) {
			w->wc_flags |= IB_WC_WITH_INVALIDATE;
			w->ex.invalidate_rkey = c.ex;
		}
		if (c.flags & SHMRDMA_CQE_GRH) {
			w->wc_flags |= IB_WC_GRH | IB_WC_WITH_NETWORK_HDR_TYPE;
			w->network_hdr_type = (c.flags & SHMRDMA_CQE_IPV4) ?
				RDMA_NETWORK_IPV4 : RDMA_NETWORK_IPV6;
		}
		n++;
	}
	smp_store_release(&cq->rec->cons, cq->cons);
	if (reset && n < num && cq->cons == prod)
		n += shmrdma_flush_cq(dev, cq, num - n, wc + n);
	spin_unlock_irqrestore(&cq->lock, flags);
	return n;
}

static int shmrdma_req_notify_cq(struct ib_cq *ibcq,
				 enum ib_cq_notify_flags flags)
{
	struct shmrdma_dev *dev = to_sdev(ibcq->device);
	struct shmrdma_cq *cq = to_scq(ibcq);
	u32 arm = (flags & IB_CQ_SOLICITED_MASK) == IB_CQ_SOLICITED ?
		  SHMRDMA_CQ_ARM_SOLICITED : SHMRDMA_CQ_ARM_NEXT;

	if (arm == SHMRDMA_CQ_ARM_SOLICITED &&
	    READ_ONCE(cq->rec->arm) == SHMRDMA_CQ_ARM_NEXT)
		arm = SHMRDMA_CQ_ARM_NEXT;
	WRITE_ONCE(cq->rec->arm, arm);
	smp_mb();
	if (smp_load_acquire(&dev->reset_done)) {
		/* no event will come: report the flushes, or deliver one */
		bool pending = shmrdma_cq_pending(dev, cq);

		if (pending && (flags & IB_CQ_REPORT_MISSED_EVENTS))
			return 1;
		if (pending)
			schedule_work(&dev->flush_work);
		return 0;
	}
	if (flags & IB_CQ_REPORT_MISSED_EVENTS) {
		unsigned long irqf;
		bool missed;

		spin_lock_irqsave(&cq->lock, irqf);
		missed = cq->cons != shmrdma_cq_prod(dev, cq);
		spin_unlock_irqrestore(&cq->lock, irqf);
		return missed;
	}
	return 0;
}

/* ---------------------------------------------------------------- */
/* queue pairs                                                      */

static void shmrdma_qp_erase(struct shmrdma_dev *dev, struct shmrdma_qp *qp);
static u32 acc_bits(int ib);

static void shmrdma_cq_link(struct shmrdma_cq *cq, struct list_head *node,
			    struct list_head *head)
{
	unsigned long flags;

	spin_lock_irqsave(&cq->lock, flags);
	list_add_tail(node, head);
	spin_unlock_irqrestore(&cq->lock, flags);
}

static int shmrdma_create_qp(struct ib_qp *ibqp,
			     struct ib_qp_init_attr *init,
			     struct ib_udata *udata)
{
	struct shmrdma_dev *dev = to_sdev(ibqp->device);
	struct shmrdma_qp *qp = to_sqp(ibqp);
	struct shmrdma_cmd cmd = { .cmd = SHMRDMA_CMD_CREATE_QP };
	struct ib_qp_cap *cap = &init->cap;
	u32 sq_entries, rq_entries, sq_esz, rq_esz, qpn;
	int rc;

	if (udata || init->srq || init->create_flags)
		return -EOPNOTSUPP;
	if (!init->send_cq || !init->recv_cq)
		return -EINVAL;
	switch (init->qp_type) {
	case IB_QPT_RC:
		qp->type = SHMRDMA_QPT_RC;
		break;
	case IB_QPT_UD:
		qp->type = SHMRDMA_QPT_UD;
		break;
	case IB_QPT_GSI:
		qp->type = SHMRDMA_QPT_GSI;
		break;
	default:
		return -EOPNOTSUPP;
	}
	if (cap->max_send_wr > SHMRDMA_MAX_QP_WR ||
	    cap->max_recv_wr > SHMRDMA_MAX_QP_WR ||
	    cap->max_send_sge > SHMRDMA_MAX_SGE ||
	    cap->max_recv_sge > SHMRDMA_MAX_SGE ||
	    cap->max_inline_data > 4096 - SHMRDMA_WQE_HDR)
		return -EINVAL;
	/* a UD WQE's address fields overlap the inline length */
	if (qp->type != SHMRDMA_QPT_RC && cap->max_inline_data)
		return -EINVAL;

	sq_entries = roundup_pow_of_two(max(cap->max_send_wr, 1u));
	rq_entries = roundup_pow_of_two(max(cap->max_recv_wr, 1u));
	sq_esz = roundup_pow_of_two(SHMRDMA_WQE_HDR +
				    max(16 * max(cap->max_send_sge, 1u),
					cap->max_inline_data));
	rq_esz = roundup_pow_of_two(sizeof(struct shmrdma_rq_wqe) +
				    16 * max(cap->max_recv_sge, 1u));

	if (qp->type == SHMRDMA_QPT_GSI) {
		qpn = 1;
	} else {
		rc = ida_alloc_rr(&dev->qpn_ida, &dev->next_qpn, 2,
				  SHMRDMA_MAX_QP - 1);
		if (rc < 0)
			return rc;
		qpn = rc;
	}
	qp->ibqp.qp_num = qpn;
	spin_lock_init(&qp->sq_lock);
	spin_lock_init(&qp->rq_lock);
	mutex_init(&qp->state_lock);
	INIT_WORK(&qp->ev_work, shmrdma_qp_ev_work);
	qp->sig_all = init->sq_sig_type == IB_SIGNAL_ALL_WR;
	qp->max_send_sge = (sq_esz - SHMRDMA_WQE_HDR) / 16;
	if (qp->max_send_sge > SHMRDMA_MAX_SGE)
		qp->max_send_sge = SHMRDMA_MAX_SGE;
	qp->max_recv_sge = (rq_esz - sizeof(struct shmrdma_rq_wqe)) / 16;
	if (qp->max_recv_sge > SHMRDMA_MAX_SGE)
		qp->max_recv_sge = SHMRDMA_MAX_SGE;
	qp->max_inline = qp->type == SHMRDMA_QPT_RC ? sq_esz - SHMRDMA_WQE_HDR : 0;
	qp->attr.qp_state = IB_QPS_RESET;
	qp->scq = to_scq(init->send_cq);
	qp->rcq = to_scq(init->recv_cq);

	qp->rec = kzalloc(sizeof(*qp->rec), GFP_KERNEL);
	if (!qp->rec) {
		rc = -ENOMEM;
		goto err_qpn;
	}
	rc = ring_alloc(&qp->sq, sq_entries, sq_esz);
	if (rc)
		goto err_rec;
	rc = ring_alloc(&qp->rq, rq_entries, rq_esz);
	if (rc)
		goto err_sq;

	cmd.create_qp.qpn = qpn;
	cmd.create_qp.type = qp->type;
	cmd.create_qp.send_cqn = qp->scq->cqn;
	cmd.create_qp.recv_cqn = qp->rcq->cqn;
	cmd.create_qp.rec_gpa = virt_to_phys(qp->rec);
	cmd.create_qp.pdn = to_spd(ibqp->pd)->pdn;
	ring_desc(&qp->sq, &cmd.create_qp.sq);
	ring_desc(&qp->rq, &cmd.create_qp.rq);
	rc = xa_insert_irq(&dev->qps, qpn, qp, GFP_KERNEL);
	if (rc)
		goto err_rq;
	shmrdma_cq_link(qp->scq, &qp->scq_node, &qp->scq->sq_qps);
	shmrdma_cq_link(qp->rcq, &qp->rcq_node, &qp->rcq->rq_qps);
	rc = shmrdma_cmd(dev, &cmd, NULL);
	if (rc)
		goto err_xa;

	cap->max_send_wr = sq_entries;
	cap->max_recv_wr = rq_entries;
	cap->max_send_sge = qp->max_send_sge;
	cap->max_recv_sge = qp->max_recv_sge;
	cap->max_inline_data = qp->max_inline;
	qp->attr.cap = *cap;
	return 0;

err_xa:
	shmrdma_qp_erase(dev, qp);
err_rq:
	ring_free(&qp->rq);
err_sq:
	ring_free(&qp->sq);
err_rec:
	kfree(qp->rec);
err_qpn:
	if (qpn != 1)
		ida_free(&dev->qpn_ida, qpn);
	return rc;
}

/* on a reset device; RESET drops what is posted, as the backend does */
static void shmrdma_cq_clean(struct shmrdma_cq *cq, struct shmrdma_qp *qp,
			     bool unlink);

static int shmrdma_qp_drop(struct shmrdma_qp *qp, enum ib_qp_state next)
{
	unsigned long flags;

	if (next != IB_QPS_RESET)
		return 0;
	spin_lock_irqsave(&qp->sq_lock, flags);
	WRITE_ONCE(qp->rec->sq_cons, qp->sq_prod);
	WRITE_ONCE(qp->rec->sq_done, qp->sq_prod);
	spin_unlock_irqrestore(&qp->sq_lock, flags);
	spin_lock_irqsave(&qp->rq_lock, flags);
	WRITE_ONCE(qp->rec->rq_cons, qp->rq_prod);
	WRITE_ONCE(qp->rec->rq_done, qp->rq_prod);
	spin_unlock_irqrestore(&qp->rq_lock, flags);
	return 0;
}

static int shmrdma_modify_qp(struct ib_qp *ibqp, struct ib_qp_attr *attr,
			     int mask, struct ib_udata *udata)
{
	struct shmrdma_dev *dev = to_sdev(ibqp->device);
	struct shmrdma_qp *qp = to_sqp(ibqp);
	struct shmrdma_cmd cmd = { .cmd = SHMRDMA_CMD_MODIFY_QP };
	enum ib_qp_state cur, next;
	int rc;

	if (udata)
		return -EOPNOTSUPP;
	mutex_lock(&qp->state_lock);
	/* a failed completion moved it to error with no event */
	if ((READ_ONCE(qp->rec->fatal) || READ_ONCE(qp->rec->err)) &&
	    qp->attr.qp_state != IB_QPS_RESET)
		qp->attr.qp_state = qp->attr.cur_qp_state = IB_QPS_ERR;
	cur = (mask & IB_QP_CUR_STATE) ? attr->cur_qp_state :
					qp->attr.qp_state;
	next = (mask & IB_QP_STATE) ? attr->qp_state : cur;
	if (!ib_modify_qp_is_ok(cur, next, ibqp->qp_type, mask)) {
		rc = -EINVAL;
		goto out;
	}
	/* the backend has no send queue drain */
	if (next == IB_QPS_SQD) {
		rc = -EOPNOTSUPP;
		goto out;
	}

	cmd.modify_qp.qpn = ibqp->qp_num;
	if (mask & IB_QP_STATE) {
		cmd.modify_qp.mask |= SHMRDMA_MQ_STATE;
		cmd.modify_qp.state = next;
	}
	if (mask & (IB_QP_AV | IB_QP_DEST_QPN)) {
		cmd.modify_qp.mask |= SHMRDMA_MQ_DEST;
		memcpy(cmd.modify_qp.dgid, mask & IB_QP_AV ?
		       rdma_ah_read_grh(&attr->ah_attr)->dgid.raw : qp->dgid,
		       16);
		cmd.modify_qp.dest_qpn = mask & IB_QP_DEST_QPN ?
					 attr->dest_qp_num :
					 qp->attr.dest_qp_num;
	}
	if (mask & IB_QP_QKEY) {
		cmd.modify_qp.mask |= SHMRDMA_MQ_QKEY;
		cmd.modify_qp.qkey = attr->qkey;
	}
	if (mask & IB_QP_RNR_RETRY) {
		cmd.modify_qp.mask |= SHMRDMA_MQ_RETRY;
		cmd.modify_qp.rnr_retry = attr->rnr_retry;
	}
	if (mask & IB_QP_ACCESS_FLAGS) {
		cmd.modify_qp.mask |= SHMRDMA_MQ_ACCESS;
		cmd.modify_qp.access = acc_bits(attr->qp_access_flags);
	}
	if (mask & (IB_QP_TIMEOUT | IB_QP_RETRY_CNT)) {
		cmd.modify_qp.mask |= SHMRDMA_MQ_TIMEOUT;
		cmd.modify_qp.timeout = mask & IB_QP_TIMEOUT ? attr->timeout :
					qp->attr.timeout;
		cmd.modify_qp.retry_cnt = mask & IB_QP_RETRY_CNT ?
					  attr->retry_cnt : qp->attr.retry_cnt;
	}

	if (cmd.modify_qp.mask) {
		rc = shmrdma_cmd(dev, &cmd, NULL);
		/* a reset device holds no QPs: going down needs nothing of it */
		if (rc && smp_load_acquire(&dev->reset_done) &&
		    (mask & IB_QP_STATE) &&
		    (next == IB_QPS_ERR || next == IB_QPS_RESET))
			rc = shmrdma_qp_drop(qp, next);
		if (rc)
			goto out;
	}

	/* only what the backend took: query_qp and later modifies read it */
	if (mask & IB_QP_AV)
		memcpy(qp->dgid, rdma_ah_read_grh(&attr->ah_attr)->dgid.raw,
		       16);
	if (mask & IB_QP_DEST_QPN)
		qp->attr.dest_qp_num = attr->dest_qp_num;
	if (mask & IB_QP_QKEY)
		qp->attr.qkey = attr->qkey;
	if (mask & IB_QP_RNR_RETRY)
		qp->attr.rnr_retry = attr->rnr_retry;
	if (mask & IB_QP_ACCESS_FLAGS)
		qp->attr.qp_access_flags = attr->qp_access_flags;
	if (mask & IB_QP_RETRY_CNT)
		qp->attr.retry_cnt = attr->retry_cnt;
	if (mask & IB_QP_TIMEOUT)
		qp->attr.timeout = attr->timeout;
	if (mask & IB_QP_PATH_MTU)
		qp->attr.path_mtu = attr->path_mtu;
	if (mask & IB_QP_SQ_PSN)
		qp->attr.sq_psn = attr->sq_psn;
	if (mask & IB_QP_RQ_PSN)
		qp->attr.rq_psn = attr->rq_psn;
	if (mask & IB_QP_PKEY_INDEX)
		qp->attr.pkey_index = attr->pkey_index;
	if (mask & IB_QP_PORT)
		qp->attr.port_num = attr->port_num;
	if (mask & IB_QP_MIN_RNR_TIMER)
		qp->attr.min_rnr_timer = attr->min_rnr_timer;
	if (mask & IB_QP_MAX_QP_RD_ATOMIC)
		qp->attr.max_rd_atomic = attr->max_rd_atomic;
	if (mask & IB_QP_MAX_DEST_RD_ATOMIC)
		qp->attr.max_dest_rd_atomic = attr->max_dest_rd_atomic;
	if (mask & IB_QP_STATE) {
		if (next == IB_QPS_RESET) {
			unsigned long flags;

			/* a QP_FATAL handler already past its rec->fatal check
			 * must not set ERR after this */
			spin_lock_irqsave(&dev->eq_lock, flags);
			qp->attr.qp_state = next;
			qp->attr.cur_qp_state = next;
			qp->fatal_seen = false;
			spin_unlock_irqrestore(&dev->eq_lock, flags);
			shmrdma_cq_clean(qp->scq, qp, false);
			if (qp->rcq != qp->scq)
				shmrdma_cq_clean(qp->rcq, qp, false);
		} else {
			qp->attr.qp_state = next;
			qp->attr.cur_qp_state = next;
		}
	}
	rc = 0;
out:
	mutex_unlock(&qp->state_lock);
	return rc;
}

static int shmrdma_query_qp(struct ib_qp *ibqp, struct ib_qp_attr *attr,
			    int mask, struct ib_qp_init_attr *init)
{
	struct shmrdma_qp *qp = to_sqp(ibqp);

	mutex_lock(&qp->state_lock);
	if ((READ_ONCE(qp->rec->fatal) || READ_ONCE(qp->rec->err)) &&
	    qp->attr.qp_state != IB_QPS_RESET)
		qp->attr.qp_state = qp->attr.cur_qp_state = IB_QPS_ERR;
	*attr = qp->attr;
	memset(&attr->ah_attr, 0, sizeof(attr->ah_attr));
	memset(&attr->alt_ah_attr, 0, sizeof(attr->alt_ah_attr));
	mutex_unlock(&qp->state_lock);
	memset(init, 0, sizeof(*init));
	init->qp_type = ibqp->qp_type;
	init->send_cq = ibqp->send_cq;
	init->recv_cq = ibqp->recv_cq;
	init->cap = qp->attr.cap;
	init->sq_sig_type = qp->sig_all ? IB_SIGNAL_ALL_WR : IB_SIGNAL_REQ_WR;
	return 0;
}

/*
 * The backend no longer completes anything for qpn: its unpolled CQEs
 * must not reach a later QP given the same number, or the QP after a
 * RESET; once unlinked, no flush finds the QP on the CQ's lists.
 */
static void shmrdma_cq_clean(struct shmrdma_cq *cq, struct shmrdma_qp *qp,
			     bool unlink)
{
	u32 qpn = qp->ibqp.qp_num;
	unsigned long flags;
	u32 i, prod;

	spin_lock_irqsave(&cq->lock, flags);
	if (unlink && qp->scq == cq)
		list_del_init(&qp->scq_node);
	if (unlink && qp->rcq == cq)
		list_del_init(&qp->rcq_node);
	prod = smp_load_acquire(&cq->rec->prod);
	if (prod - cq->cons > cq->ring.entries)
		prod = cq->cons + cq->ring.entries;
	for (i = cq->cons; i != prod; i++) {
		struct shmrdma_cqe *e = ring_at(&cq->ring, i);

		if (READ_ONCE(e->qpn) == qpn)
			WRITE_ONCE(e->qpn, SHMRDMA_QPN_NONE);
	}
	spin_unlock_irqrestore(&cq->lock, flags);
}

/*
 * No event handler runs for qp after this, and no poll_cq, which looks
 * QPs up under the CQ's lock, still uses it.
 */
static void shmrdma_qp_erase(struct shmrdma_dev *dev, struct shmrdma_qp *qp)
{
	unsigned long flags;

	spin_lock_irqsave(&dev->eq_lock, flags);
	xa_erase(&dev->qps, qp->ibqp.qp_num);
	shmrdma_cq_clean(qp->scq, qp, true);
	if (qp->rcq != qp->scq)
		shmrdma_cq_clean(qp->rcq, qp, true);
	spin_unlock_irqrestore(&dev->eq_lock, flags);
	cancel_work_sync(&qp->ev_work);
}

static int shmrdma_destroy_qp(struct ib_qp *ibqp, struct ib_udata *udata)
{
	struct shmrdma_dev *dev = to_sdev(ibqp->device);
	struct shmrdma_qp *qp = to_sqp(ibqp);
	struct shmrdma_cmd cmd = { .cmd = SHMRDMA_CMD_DESTROY_QP };
	int rc;

	cmd.destroy_qp.qpn = ibqp->qp_num;
	rc = shmrdma_cmd(dev, &cmd, NULL);
	if (rc)
		shmrdma_destroy_failed(dev, "destroy qp", ibqp->qp_num, rc);
	shmrdma_qp_erase(dev, qp);
	ring_free(&qp->sq);
	ring_free(&qp->rq);
	kfree(qp->rec);
	if (ibqp->qp_num != 1)
		ida_free(&dev->qpn_ida, ibqp->qp_num);
	return 0;
}

static u32 acc_bits(int ib)
{
	u32 a = 0;

	if (ib & IB_ACCESS_LOCAL_WRITE)
		a |= SHMRDMA_ACC_LOCAL_WRITE;
	if (ib & IB_ACCESS_REMOTE_WRITE)
		a |= SHMRDMA_ACC_REMOTE_WRITE;
	if (ib & IB_ACCESS_REMOTE_READ)
		a |= SHMRDMA_ACC_REMOTE_READ;
	return a;
}

static int build_wqe(struct shmrdma_qp *qp, const struct ib_send_wr *wr,
		     struct shmrdma_sq_wqe *w)
{
	int i;

	memset(w, 0, SHMRDMA_WQE_HDR);
	w->wr_id = wr->wr_id;
	if ((wr->send_flags & IB_SEND_SIGNALED) || qp->sig_all)
		w->flags |= SHMRDMA_WQE_SIGNALED;
	if (wr->send_flags & IB_SEND_SOLICITED)
		w->flags |= SHMRDMA_WQE_SOLICITED;
	if (wr->send_flags & IB_SEND_FENCE)
		w->flags |= SHMRDMA_WQE_FENCE;

	switch (wr->opcode) {
	case IB_WR_SEND:
		w->opcode = SHMRDMA_OP_SEND;
		break;
	case IB_WR_SEND_WITH_IMM:
		w->opcode = SHMRDMA_OP_SEND_IMM;
		w->ex = (__force u32)wr->ex.imm_data;
		break;
	case IB_WR_SEND_WITH_INV:
		w->opcode = SHMRDMA_OP_SEND_INV;
		w->ex = wr->ex.invalidate_rkey;
		break;
	case IB_WR_RDMA_WRITE:
		w->opcode = SHMRDMA_OP_RDMA_WRITE;
		break;
	case IB_WR_RDMA_WRITE_WITH_IMM:
		w->opcode = SHMRDMA_OP_RDMA_WRITE_IMM;
		w->ex = (__force u32)wr->ex.imm_data;
		break;
	case IB_WR_RDMA_READ:
		w->opcode = SHMRDMA_OP_RDMA_READ;
		break;
	case IB_WR_REG_MR: {
		const struct ib_reg_wr *rw = reg_wr(wr);
		struct shmrdma_mr *mr = to_smr(rw->mr);

		/* the backend puts the MR in the QP's PD */
		if (rw->mr->pd != qp->ibqp.pd)
			return -EINVAL;
		w->opcode = SHMRDMA_OP_REG_MR;
		w->reg.iova = mr->ibmr.iova;
		w->reg.length = mr->ibmr.length;
		w->reg.pages_gpa = virt_to_phys(mr->pages);
		w->reg.key = rw->key;
		w->reg.npages = mr->npages;
		w->reg.access = acc_bits(rw->access);
		w->reg.page_shift = ilog2(mr->ibmr.page_size);
		w->ex = mr->fpo;
		return 0;
	}
	case IB_WR_LOCAL_INV:
		w->opcode = SHMRDMA_OP_LOCAL_INV;
		w->ex = wr->ex.invalidate_rkey;
		return 0;
	default:
		return -EINVAL;
	}

	if (qp->type != SHMRDMA_QPT_RC) {
		const struct ib_ud_wr *uw = ud_wr(wr);
		struct shmrdma_ah *ah = to_sah(uw->ah);

		if (w->opcode != SHMRDMA_OP_SEND &&
		    w->opcode != SHMRDMA_OP_SEND_IMM)
			return -EINVAL;
		w->ud.dqpn = uw->remote_qpn;
		w->ud.qkey = uw->remote_qkey;
		memcpy(w->ud.dgid, ah->dgid, 16);
		memcpy(w->ud.sgid, ah->sgid, 16);
	} else if (w->opcode == SHMRDMA_OP_RDMA_WRITE ||
		   w->opcode == SHMRDMA_OP_RDMA_WRITE_IMM ||
		   w->opcode == SHMRDMA_OP_RDMA_READ) {
		w->rdma.remote_addr = rdma_wr(wr)->remote_addr;
		w->rdma.rkey = rdma_wr(wr)->rkey;
	}

	if ((wr->send_flags & IB_SEND_INLINE) &&
	    w->opcode != SHMRDMA_OP_RDMA_READ) {
		u8 *p = (u8 *)w->sge;
		u32 len = 0;

		for (i = 0; i < wr->num_sge; i++) {
			if (wr->sg_list[i].length > qp->max_inline - len)
				return -EINVAL;
			memcpy(p + len, (void *)(uintptr_t)wr->sg_list[i].addr,
			       wr->sg_list[i].length);
			len += wr->sg_list[i].length;
		}
		w->flags |= SHMRDMA_WQE_INLINE;
		if (qp->type == SHMRDMA_QPT_RC &&
		    (w->opcode == SHMRDMA_OP_RDMA_WRITE ||
		     w->opcode == SHMRDMA_OP_RDMA_WRITE_IMM))
			w->rdma.inline_len = len;
		else if (qp->type == SHMRDMA_QPT_RC)
			w->send.inline_len = len;
		else
			return -EINVAL;	/* inline UD: overlaps ud fields */
		return 0;
	}
	if (wr->num_sge > qp->max_send_sge)
		return -EINVAL;
	w->num_sge = wr->num_sge;
	for (i = 0; i < wr->num_sge; i++) {
		w->sge[i].addr = wr->sg_list[i].addr;
		w->sge[i].length = wr->sg_list[i].length;
		w->sge[i].lkey = wr->sg_list[i].lkey;
	}
	return 0;
}

static int shmrdma_post_send(struct ib_qp *ibqp, const struct ib_send_wr *wr,
			     const struct ib_send_wr **bad)
{
	struct shmrdma_dev *dev = to_sdev(ibqp->device);
	struct shmrdma_qp *qp = to_sqp(ibqp);
	enum ib_qp_state st = READ_ONCE(qp->attr.qp_state);
	unsigned long flags;
	int rc = 0;

	if (st != IB_QPS_RTS && st != IB_QPS_ERR) {
		*bad = wr;
		return -EINVAL;
	}
	spin_lock_irqsave(&qp->sq_lock, flags);
	for (; wr; wr = wr->next) {
		if (qp->sq_prod - READ_ONCE(qp->rec->sq_cons) >=
		    qp->sq.entries) {
			rc = -ENOMEM;
			break;
		}
		rc = build_wqe(qp, wr, ring_at(&qp->sq, qp->sq_prod));
		if (rc)
			break;
		qp->sq_prod++;
	}
	smp_store_release(&qp->rec->sq_prod, qp->sq_prod);
	spin_unlock_irqrestore(&qp->sq_lock, flags);
	if (smp_load_acquire(&dev->reset_done))
		schedule_work(&dev->flush_work);
	else
		shmrdma_doorbell(dev, qp->rec);
	if (rc)
		*bad = wr;
	return rc;
}

static int shmrdma_post_recv(struct ib_qp *ibqp, const struct ib_recv_wr *wr,
			     const struct ib_recv_wr **bad)
{
	struct shmrdma_dev *dev = to_sdev(ibqp->device);
	struct shmrdma_qp *qp = to_sqp(ibqp);
	unsigned long flags;
	int rc = 0;
	int i;

	if (READ_ONCE(qp->attr.qp_state) == IB_QPS_RESET) {
		*bad = wr;
		return -EINVAL;
	}
	spin_lock_irqsave(&qp->rq_lock, flags);
	for (; wr; wr = wr->next) {
		struct shmrdma_rq_wqe *w;

		if (qp->rq_prod - READ_ONCE(qp->rec->rq_cons) >=
		    qp->rq.entries) {
			rc = -ENOMEM;
			break;
		}
		if (wr->num_sge > qp->max_recv_sge) {
			rc = -EINVAL;
			break;
		}
		w = ring_at(&qp->rq, qp->rq_prod);
		w->wr_id = wr->wr_id;
		w->num_sge = wr->num_sge;
		for (i = 0; i < wr->num_sge; i++) {
			w->sge[i].addr = wr->sg_list[i].addr;
			w->sge[i].length = wr->sg_list[i].length;
			w->sge[i].lkey = wr->sg_list[i].lkey;
		}
		qp->rq_prod++;
	}
	smp_store_release(&qp->rec->rq_prod, qp->rq_prod);
	spin_unlock_irqrestore(&qp->rq_lock, flags);
	if (smp_load_acquire(&dev->reset_done))
		schedule_work(&dev->flush_work);
	else
		shmrdma_doorbell(dev, qp->rec);
	if (rc)
		*bad = wr;
	return rc;
}

/* ---------------------------------------------------------------- */
/* memory regions                                                   */

static struct ib_mr *shmrdma_get_dma_mr(struct ib_pd *pd, int access)
{
	struct shmrdma_mr *mr;

	/* the backend never takes key 0 from a remote peer */
	if (access & (IB_ACCESS_REMOTE_READ | IB_ACCESS_REMOTE_WRITE |
		      IB_ACCESS_REMOTE_ATOMIC))
		return ERR_PTR(-EOPNOTSUPP);
	mr = kzalloc(sizeof(*mr), GFP_KERNEL);
	if (!mr)
		return ERR_PTR(-ENOMEM);
	mr->ibmr.lkey = SHMRDMA_LOCAL_DMA_LKEY;
	mr->ibmr.rkey = SHMRDMA_LOCAL_DMA_LKEY;
	return &mr->ibmr;
}

static struct ib_mr *shmrdma_reg_user_mr(struct ib_pd *pd, u64 start, u64 len,
					 u64 virt, int access,
#ifdef HAVE_IB_DMAH
					 struct ib_dmah *dmah,
#endif
					 struct ib_udata *udata)
{
	return ERR_PTR(-EOPNOTSUPP);
}

static struct ib_mr *shmrdma_alloc_mr(struct ib_pd *pd, enum ib_mr_type type,
				      u32 max_num_sg)
{
	struct shmrdma_dev *dev = to_sdev(pd->device);
	struct shmrdma_mr *mr;
	int rc;

	if (type != IB_MR_TYPE_MEM_REG || max_num_sg > SHMRDMA_MAX_FRPL)
		return ERR_PTR(-EINVAL);
	mr = kzalloc(sizeof(*mr), GFP_KERNEL);
	if (!mr)
		return ERR_PTR(-ENOMEM);
	mr->pages = kcalloc(max_num_sg, sizeof(u64), GFP_KERNEL);
	if (!mr->pages) {
		kfree(mr);
		return ERR_PTR(-ENOMEM);
	}
	rc = ida_alloc_rr(&dev->mr_ida, &dev->next_mrn, 1, SHMRDMA_MAX_MR - 1);
	if (rc < 0) {
		kfree(mr->pages);
		kfree(mr);
		return ERR_PTR(rc);
	}
	mr->index = rc;
	mr->max_pages = max_num_sg;
	mr->ibmr.lkey = mr->index << 8 |
			(u8)atomic_inc_return(&dev->mr_variant);
	mr->ibmr.rkey = mr->ibmr.lkey;
	return &mr->ibmr;
}

static int shmrdma_set_page(struct ib_mr *ibmr, u64 addr)
{
	struct shmrdma_mr *mr = to_smr(ibmr);

	if (mr->npages >= mr->max_pages)
		return -ENOMEM;
	mr->pages[mr->npages++] = addr;
	return 0;
}

static int shmrdma_map_mr_sg(struct ib_mr *ibmr, struct scatterlist *sg,
			     int sg_nents, unsigned int *sg_offset)
{
	struct shmrdma_mr *mr = to_smr(ibmr);
	int n;

	mr->npages = 0;
	n = ib_sg_to_pages(ibmr, sg, sg_nents, sg_offset, shmrdma_set_page);
	mr->fpo = ibmr->iova & (ibmr->page_size - 1);
	return n;
}

static int shmrdma_dereg_mr(struct ib_mr *ibmr, struct ib_udata *udata)
{
	struct shmrdma_dev *dev = to_sdev(ibmr->device);
	struct shmrdma_mr *mr = to_smr(ibmr);
	int rc;

	if (mr->index) {
		struct shmrdma_cmd cmd = { .cmd = SHMRDMA_CMD_DEREG_MR };

		cmd.dereg_mr.key = ibmr->lkey;
		rc = shmrdma_cmd(dev, &cmd, NULL);
		if (rc)
			shmrdma_destroy_failed(dev, "dereg mr", mr->index, rc);
		ida_free(&dev->mr_ida, mr->index);
	}
	kfree(mr->pages);
	kfree(mr);
	return 0;
}

static const struct ib_device_ops shmrdma_dev_ops = {
	.owner = THIS_MODULE,
	.driver_id = RDMA_DRIVER_UNKNOWN,
	.uverbs_abi_ver = 1,

	.add_gid = shmrdma_add_gid,
	.alloc_mr = shmrdma_alloc_mr,
	.alloc_pd = shmrdma_alloc_pd,
	.create_ah = shmrdma_create_ah,
	.create_cq = shmrdma_create_cq,
	.create_qp = shmrdma_create_qp,
	.dealloc_pd = shmrdma_dealloc_pd,
	.del_gid = shmrdma_del_gid,
	.dereg_mr = shmrdma_dereg_mr,
	.destroy_ah = shmrdma_destroy_ah,
	.destroy_cq = shmrdma_destroy_cq,
	.destroy_qp = shmrdma_destroy_qp,
	.get_dma_mr = shmrdma_get_dma_mr,
	.get_link_layer = shmrdma_link_layer,
	.get_port_immutable = shmrdma_get_port_immutable,
	.map_mr_sg = shmrdma_map_mr_sg,
	.modify_qp = shmrdma_modify_qp,
	.poll_cq = shmrdma_poll_cq,
	.post_recv = shmrdma_post_recv,
	.post_send = shmrdma_post_send,
	.query_device = shmrdma_query_device,
	.query_pkey = shmrdma_query_pkey,
	.query_port = shmrdma_query_port,
	.query_qp = shmrdma_query_qp,
	.reg_user_mr = shmrdma_reg_user_mr,
	.req_notify_cq = shmrdma_req_notify_cq,

	INIT_RDMA_OBJ_SIZE(ib_ah, shmrdma_ah, ibah),
	INIT_RDMA_OBJ_SIZE(ib_cq, shmrdma_cq, ibcq),
	INIT_RDMA_OBJ_SIZE(ib_pd, shmrdma_pd, ibpd),
	INIT_RDMA_OBJ_SIZE(ib_qp, shmrdma_qp, ibqp),
};

/* ---------------------------------------------------------------- */
/* virtio glue                                                      */

static void shmrdma_init_attr(struct shmrdma_dev *dev)
{
	struct ib_device_attr *a = &dev->attr;

	memset(a, 0, sizeof(*a));
	a->max_mr_size = 1ull << 40;
	a->page_size_cap = 0x7ffff000;	/* the backend takes 4 KB to 1 GB */
	a->vendor_id = 0x1af4;
	a->vendor_part_id = SHMRDMA_VIRTIO_ID;
	a->hw_ver = SHMRDMA_ABI_VERSION;
	a->max_qp = SHMRDMA_MAX_QP - 2;
	a->max_qp_wr = SHMRDMA_MAX_QP_WR;
	a->device_cap_flags = IB_DEVICE_MEM_MGT_EXTENSIONS |
			      IB_DEVICE_RC_RNR_NAK_GEN;
	a->kernel_cap_flags = IBK_LOCAL_DMA_LKEY;
	a->max_send_sge = SHMRDMA_MAX_SGE;
	a->max_recv_sge = SHMRDMA_MAX_SGE;
	a->max_sge_rd = SHMRDMA_MAX_SGE;
	a->max_cq = SHMRDMA_MAX_CQ;
	a->max_cqe = SHMRDMA_MAX_CQE - 1;
	a->max_mr = SHMRDMA_MAX_MR - 1;
	a->max_pd = SHMRDMA_MAX_PD - 1;
	a->max_qp_rd_atom = 16;
	a->max_res_rd_atom = 16 * (SHMRDMA_MAX_QP - 2);
	a->max_qp_init_rd_atom = 16;
	a->atomic_cap = IB_ATOMIC_NONE;
	a->max_ah = 1 << 20;
	a->max_fast_reg_page_list_len = SHMRDMA_MAX_FRPL;
	a->max_pkeys = 1;
	a->local_ca_ack_delay = 15;
	a->sys_image_guid = dev->ibdev.node_guid;
}

/* After the notifier is gone, or before it was registered. */
static void shmrdma_put_ndev(struct shmrdma_dev *dev)
{
	struct net_device *ndev = rcu_dereference_protected(dev->ndev, 1);

	RCU_INIT_POINTER(dev->ndev, NULL);
	if (ndev)
		dev_put(ndev);
}

/*
 * ib_device_set_netdev()'s contract: on NETDEV_UNREGISTER, detach the
 * netdev, or its unregistration waits forever for our references.
 */
static int shmrdma_netdev_event(struct notifier_block *nb,
				unsigned long event, void *ptr)
{
	struct shmrdma_dev *dev = container_of(nb, struct shmrdma_dev, nb);
	struct net_device *ndev = netdev_notifier_info_to_dev(ptr);

	/* its NIC rebound (rc.local's NUMA setup does that) */
	if ((event == NETDEV_REGISTER || event == NETDEV_CHANGENAME) &&
	    !rtnl_dereference(dev->ndev) && READ_ONCE(dev->registered) &&
	    net_eq(dev_net(ndev), &init_net) &&
	    !strcmp(ndev->name, dev->ifname) &&
	    !ib_device_set_netdev(&dev->ibdev, ndev, 1)) {
		dev_hold(ndev);
		rcu_assign_pointer(dev->ndev, ndev);
		return NOTIFY_DONE;
	}
	if (ndev != rtnl_dereference(dev->ndev))
		return NOTIFY_DONE;
#ifndef HAVE_IB_DISPATCH_PORT_STATE_EVENT
	/* newer cores dispatch port events from the netdev themselves */
	if (event == NETDEV_UP || event == NETDEV_DOWN ||
	    event == NETDEV_CHANGE) {
		/* the port follows its netdev's link, as a RoCE HCA's does */
		bool up = shmrdma_port_up(dev);
		struct ib_event ev = {
			.device = &dev->ibdev,
			.event = up ? IB_EVENT_PORT_ACTIVE : IB_EVENT_PORT_ERR,
			.element.port_num = 1,
		};

		if (up != dev->port_up && READ_ONCE(dev->registered)) {
			dev->port_up = up;
			ib_dispatch_event(&ev);
		}
		return NOTIFY_DONE;
	}
#endif
	if (event != NETDEV_UNREGISTER)
		return NOTIFY_DONE;
	ib_device_set_netdev(&dev->ibdev, NULL, 1);
	RCU_INIT_POINTER(dev->ndev, NULL);
	synchronize_net();
	dev_put(ndev);
	return NOTIFY_DONE;
}

/*
 * The backend reads every address a ULP posts as a guest physical one,
 * so a DMA mapping must be the identity: no IOMMU, no bounce buffers.
 */
static int shmrdma_check_dma(struct device *dma)
{
	struct page *pg = alloc_page(GFP_KERNEL);
	dma_addr_t a;
	int rc = 0;

	if (!pg)
		return -ENOMEM;
	a = dma_map_page(dma, pg, 0, PAGE_SIZE, DMA_BIDIRECTIONAL);
	if (dma_mapping_error(dma, a)) {
		rc = -EIO;
	} else {
		if (a != page_to_phys(pg))
			rc = -EOPNOTSUPP;
		dma_unmap_page(dma, a, PAGE_SIZE, DMA_BIDIRECTIONAL);
	}
	__free_page(pg);
	return rc;
}

static int shmrdma_probe(struct virtio_device *vdev)
{
#ifdef HAVE_VIRTQUEUE_INFO
	struct virtqueue_info vqi[SHMRDMA_NUM_VQS] = {
		[SHMRDMA_VQ_CTRL] = { "ctrl", shmrdma_ctrl_cb },
		[SHMRDMA_VQ_EVENT] = { "event", shmrdma_event_cb },
		[SHMRDMA_VQ_DOORBELL] = { "doorbell", NULL },
	};
#else
	vq_callback_t *cbs[SHMRDMA_NUM_VQS] = {
		[SHMRDMA_VQ_CTRL] = shmrdma_ctrl_cb,
		[SHMRDMA_VQ_EVENT] = shmrdma_event_cb,
	};
	const char *names[SHMRDMA_NUM_VQS] = {
		[SHMRDMA_VQ_CTRL] = "ctrl",
		[SHMRDMA_VQ_EVENT] = "event",
		[SHMRDMA_VQ_DOORBELL] = "doorbell",
	};
#endif
	struct virtqueue *vqs[SHMRDMA_NUM_VQS];
	struct shmrdma_cmd cmd = { .cmd = SHMRDMA_CMD_INIT };
	struct shmrdma_resp resp = { 0 };
	struct shmrdma_dev *dev;
	struct net_device *ndev;
	struct scatterlist sg;
	int rc, i;

	char tag[sizeof_field(struct virtio_fs_config, tag) + 1] = "";
	const char *ifname = netdev;

	virtio_cread_bytes(vdev, offsetof(struct virtio_fs_config, tag), tag,
			   sizeof(tag) - 1);
	/* "shmrdma", or "shmrdma@IFNAME" for a device bound to IFNAME */
	if (!strncmp(tag, SHMRDMA_TAG "@", sizeof(SHMRDMA_TAG)) &&
	    tag[sizeof(SHMRDMA_TAG)])
		ifname = tag + sizeof(SHMRDMA_TAG);
	else if (strcmp(tag, SHMRDMA_TAG))
		return -ENODEV;
	rc = shmrdma_check_dma(vdev->dev.parent);
	if (rc) {
		dev_err(&vdev->dev,
			"DMA addresses are not guest physical (IOMMU or bounce buffers): rc = %d\n",
			rc);
		return rc;
	}

	/*
	 * Its NIC may probe after it: try again when another device binds.
	 * virtiofs, loading after this module, may take it meanwhile; the
	 * boot hook (shmrdma-boot.sh) hands it back.  Reset it first, so a
	 * later probe starts from a device with no queues set up.
	 */
	ndev = dev_get_by_name(&init_net, ifname);
	if (!ndev) {
		dev_info(&vdev->dev, "no netdev %s yet\n", ifname);
		virtio_reset_device(vdev);
		virtio_add_status(vdev, VIRTIO_CONFIG_S_ACKNOWLEDGE);
		return -EPROBE_DEFER;
	}
	dev = ib_alloc_device(shmrdma_dev, ibdev);
	if (!dev) {
		dev_put(ndev);
		return -ENOMEM;
	}
	dev->vdev = vdev;
	strscpy(dev->ifname, ifname, sizeof(dev->ifname));
	RCU_INIT_POINTER(dev->ndev, ndev);
	vdev->priv = dev;
	mutex_init(&dev->ctrl_mutex);
	INIT_WORK(&dev->fatal_work, shmrdma_fatal_work);
	INIT_WORK(&dev->flush_work, shmrdma_flush_work);
	INIT_WORK(&dev->eq_work, shmrdma_eq_work);
	init_completion(&dev->ctrl_done);
	spin_lock_init(&dev->eq_lock);
	xa_init_flags(&dev->qps, XA_FLAGS_LOCK_IRQ);
	xa_init_flags(&dev->cqs, XA_FLAGS_LOCK_IRQ);
	ida_init(&dev->qpn_ida);
	ida_init(&dev->cqn_ida);
	ida_init(&dev->mr_ida);
	ida_init(&dev->pdn_ida);

	rc = -ENOMEM;
	dev->cmdbuf = kzalloc(sizeof(*dev->cmdbuf), GFP_KERNEL);
	dev->respbuf = kzalloc(sizeof(*dev->respbuf), GFP_KERNEL);
	dev->evbufs = kcalloc(SHMRDMA_NEVBUF, sizeof(u64), GFP_KERNEL);
	dev->shared = (void *)get_zeroed_page(GFP_KERNEL);
	dev->eq = alloc_pages_exact(SHMRDMA_EQ_ENTRIES *
				    sizeof(struct shmrdma_eqe),
				    GFP_KERNEL | __GFP_ZERO);
	if (!dev->cmdbuf || !dev->respbuf || !dev->evbufs || !dev->shared ||
	    !dev->eq)
		goto err_free;

#ifdef HAVE_VIRTQUEUE_INFO
	rc = virtio_find_vqs(vdev, SHMRDMA_NUM_VQS, vqs, vqi, NULL);
#else
	rc = virtio_find_vqs(vdev, SHMRDMA_NUM_VQS, vqs, cbs, names, NULL);
#endif
	if (rc)
		goto err_free;
	dev->ctrl_vq = vqs[SHMRDMA_VQ_CTRL];
	dev->event_vq = vqs[SHMRDMA_VQ_EVENT];
	dev->db_vq = vqs[SHMRDMA_VQ_DOORBELL];

	for (i = 0; i < SHMRDMA_NEVBUF; i++) {
		sg_init_one(&sg, &dev->evbufs[i], sizeof(u64));
		virtqueue_add_inbuf(dev->event_vq, &sg, 1, &dev->evbufs[i],
				    GFP_KERNEL);
	}
	virtio_device_ready(vdev);
	virtqueue_kick(dev->event_vq);

	cmd.init.abi = SHMRDMA_ABI_VERSION;
	cmd.init.eq_entries = SHMRDMA_EQ_ENTRIES;
	cmd.init.shared_gpa = virt_to_phys(dev->shared);
	cmd.init.eq_gpa = virt_to_phys(dev->eq);
	WRITE_ONCE(dev->shared->eq_armed, 1);
	rc = shmrdma_cmd(dev, &cmd, &resp);
	if (!rc && resp.abi != SHMRDMA_ABI_VERSION)
		rc = -EPROTO;
	if (rc) {
		dev_err(&vdev->dev, "backend init failed: %d (ABI %u, backend %u)\n",
			rc, SHMRDMA_ABI_VERSION, resp.abi);
		goto err_reset;
	}

	strscpy(dev->ibdev.node_desc, "shmrdma", sizeof(dev->ibdev.node_desc));
	dev->ibdev.node_type = RDMA_NODE_IB_CA;
	dev->ibdev.phys_port_cnt = 1;
	dev->ibdev.num_comp_vectors = 1;
	dev->ibdev.local_dma_lkey = SHMRDMA_LOCAL_DMA_LKEY;
	addrconf_addr_eui48((unsigned char *)&dev->ibdev.node_guid,
			    ndev->dev_addr);
	shmrdma_init_attr(dev);
	ib_set_device_ops(&dev->ibdev, &shmrdma_dev_ops);
	dev->nb.notifier_call = shmrdma_netdev_event;
	rc = register_netdevice_notifier(&dev->nb);
	if (rc)
		goto err_reset;
	dev->nb_registered = true;
	rtnl_lock();
	ndev = rtnl_dereference(dev->ndev);
	/* an unregister before the notifier was registered was not seen */
	rc = ndev && ndev->reg_state == NETREG_REGISTERED ?
	     ib_device_set_netdev(&dev->ibdev, ndev, 1) : -ENODEV;
	dev->port_up = !rc && shmrdma_port_up(dev);
	rtnl_unlock();
	if (rc)
		goto err_reset;
	rc = ib_register_device(&dev->ibdev, "shmrdma%d", vdev->dev.parent);
	if (rc) {
		dev_err(&vdev->dev, "ib_register_device: %d\n", rc);
		goto err_reset;
	}
	mutex_lock(&dev->ctrl_mutex);
	dev->registered = true;
	/* a fatal error while ib_register_device ran was not reported */
	if (dev->broken)
		schedule_work(&dev->fatal_work);
	mutex_unlock(&dev->ctrl_mutex);
	dev_info(&vdev->dev, "%s on %s, backend dev%u, ABI %u\n",
		 dev_name(&dev->ibdev.dev), ifname, (u32)resp.val,
		 SHMRDMA_ABI_VERSION);
	return 0;

err_reset:
	if (dev->nb_registered)
		unregister_netdevice_notifier(&dev->nb);
	mutex_lock(&dev->ctrl_mutex);
	shmrdma_reset_locked(dev);
	mutex_unlock(&dev->ctrl_mutex);
	cancel_work_sync(&dev->fatal_work);
	cancel_work_sync(&dev->flush_work);
	vdev->config->del_vqs(vdev);
	cancel_work_sync(&dev->eq_work);
err_free:
	if (dev->eq)
		free_pages_exact(dev->eq, SHMRDMA_EQ_ENTRIES *
				 sizeof(struct shmrdma_eqe));
	free_page((unsigned long)dev->shared);
	kfree(dev->evbufs);
	kfree(dev->respbuf);
	kfree(dev->cmdbuf);
	shmrdma_put_ndev(dev);
	ida_destroy(&dev->qpn_ida);
	ida_destroy(&dev->cqn_ida);
	ida_destroy(&dev->mr_ida);
	ida_destroy(&dev->pdn_ida);
	ib_dealloc_device(&dev->ibdev);
	return rc;
}

static void shmrdma_remove(struct virtio_device *vdev)
{
	struct shmrdma_dev *dev = vdev->priv;
	struct shmrdma_cmd cmd = { .cmd = SHMRDMA_CMD_RESET };
	bool registered;

	if (dev->nb_registered)
		unregister_netdevice_notifier(&dev->nb);
	/* no fatal event may be dispatched while or after it unregisters */
	mutex_lock(&dev->ctrl_mutex);
	registered = dev->registered;
	dev->registered = false;
	mutex_unlock(&dev->ctrl_mutex);
	flush_work(&dev->fatal_work);
	if (registered)
		ib_unregister_device(&dev->ibdev);
	shmrdma_cmd(dev, &cmd, NULL);
	mutex_lock(&dev->ctrl_mutex);
	shmrdma_reset_locked(dev);
	mutex_unlock(&dev->ctrl_mutex);
	cancel_work_sync(&dev->fatal_work);
	cancel_work_sync(&dev->flush_work);
	vdev->config->del_vqs(vdev);
	cancel_work_sync(&dev->eq_work);
	free_pages_exact(dev->eq, SHMRDMA_EQ_ENTRIES *
			 sizeof(struct shmrdma_eqe));
	free_page((unsigned long)dev->shared);
	kfree(dev->evbufs);
	kfree(dev->respbuf);
	kfree(dev->cmdbuf);
	shmrdma_put_ndev(dev);
	ida_destroy(&dev->qpn_ida);
	ida_destroy(&dev->cqn_ida);
	ida_destroy(&dev->mr_ida);
	ida_destroy(&dev->pdn_ida);
	ib_dealloc_device(&dev->ibdev);
}

#ifndef HAVE_VIRTIO_DRIVER_SHUTDOWN
/*
 * kexec: the backend must stop writing into memory the next kernel will
 * reuse.  Newer virtio cores reset every device at shutdown themselves.
 */
static void shmrdma_shutdown(struct device *d)
{
	struct virtio_device *vdev = dev_to_virtio(d);

	virtio_break_device(vdev);
	virtio_reset_device(vdev);
}
#endif

static const struct virtio_device_id id_table[] = {
	{ SHMRDMA_VIRTIO_ID, VIRTIO_DEV_ANY_ID },
	{ 0 },
};

static unsigned int features[] = { };

static struct virtio_driver shmrdma_driver = {
	.feature_table = features,
	.feature_table_size = ARRAY_SIZE(features),
	.driver.name = KBUILD_MODNAME,
#ifndef HAVE_VIRTIO_DRIVER_SHUTDOWN
	.driver.shutdown = shmrdma_shutdown,
#endif
	.id_table = id_table,
	.probe = shmrdma_probe,
	.remove = shmrdma_remove,
};

module_virtio_driver(shmrdma_driver);
MODULE_DEVICE_TABLE(virtio, id_table);
MODULE_DESCRIPTION("RDMA between VMs through host shared memory");
MODULE_LICENSE("GPL");
