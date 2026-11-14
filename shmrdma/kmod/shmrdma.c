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
#include <net/addrconf.h>
#include <rdma/ib_verbs.h>
#include <rdma/ib_mad.h>

#include "../include/shmrdma_abi.h"

static char *netdev = "eth0";
module_param(netdev, charp, 0444);
MODULE_PARM_DESC(netdev, "netdev whose IP addresses give the GIDs");

#define SHMRDMA_EQ_ENTRIES	16384
#define SHMRDMA_NEVBUF		8
#define SHMRDMA_CQE_SIZE	32

struct shmrdma_dev {
	struct ib_device ibdev;
	struct virtio_device *vdev;
	struct virtqueue *ctrl_vq;
	struct virtqueue *event_vq;
	struct virtqueue *db_vq;
	struct net_device *ndev;

	struct mutex ctrl_mutex;
	struct completion ctrl_done;
	struct shmrdma_cmd *cmdbuf;
	struct shmrdma_resp *respbuf;
	bool broken;

	spinlock_t eq_lock;
	struct shmrdma_dev_shared *shared;
	struct shmrdma_eqe *eq;
	u32 eq_cons;
	u64 *evbufs;

	struct xarray qps;
	struct xarray cqs;
	struct ida qpn_ida;
	struct ida cqn_ida;
	struct ida mr_ida;

	struct ib_device_attr attr;
	bool registered;
};

struct shmrdma_pd {
	struct ib_pd ibpd;
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
};

struct shmrdma_qp {
	struct ib_qp ibqp;
	u32 type;
	struct shmrdma_qp_rec *rec;
	struct shmrdma_ring sq, rq;
	spinlock_t sq_lock, rq_lock;
	u32 sq_prod, rq_prod;
	u32 max_send_sge, max_recv_sge, max_inline;
	bool sig_all;
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

static int shmrdma_cmd(struct shmrdma_dev *dev, struct shmrdma_cmd *cmd,
		       u64 *val)
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
			dev_err(&dev->vdev->dev, "command %u timed out\n",
				cmd->cmd);
			dev->broken = true;
			rc = -ETIMEDOUT;
			goto out;
		}
		if (virtqueue_get_buf(dev->ctrl_vq, &len))
			break;
	}
	rc = dev->respbuf->status;
	if (val)
		*val = dev->respbuf->val;
out:
	mutex_unlock(&dev->ctrl_mutex);
	return rc;
}

/* ---------------------------------------------------------------- */
/* rings                                                            */

static int ring_alloc(struct shmrdma_ring *r, u32 entries, u32 esz)
{
	size_t bytes = PAGE_ALIGN((size_t)entries * esz);
	u32 i;

	r->entries = entries;
	r->esz = esz;
	r->npages = bytes >> PAGE_SHIFT;
	r->buf = vzalloc(bytes);
	if (!r->buf)
		return -ENOMEM;
	r->pages = kmalloc_array(r->npages, sizeof(u64), GFP_KERNEL);
	if (!r->pages) {
		vfree(r->buf);
		r->buf = NULL;
		return -ENOMEM;
	}
	for (i = 0; i < r->npages; i++)
		r->pages[i] = page_to_phys(vmalloc_to_page(r->buf +
							   i * PAGE_SIZE));
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

static void shmrdma_event_cb(struct virtqueue *vq)
{
	struct shmrdma_dev *dev = vq->vdev->priv;
	struct shmrdma_dev_shared *sh = dev->shared;
	unsigned long flags;
	unsigned int len;
	struct scatterlist sg;
	void *buf;
	bool kick = false;

	spin_lock_irqsave(&dev->eq_lock, flags);
	while ((buf = virtqueue_get_buf(vq, &len)) != NULL) {
		sg_init_one(&sg, buf, sizeof(u64));
		virtqueue_add_inbuf(vq, &sg, 1, buf, GFP_ATOMIC);
		kick = true;
	}
	if (kick)
		virtqueue_kick(vq);
	if (!sh)
		goto out;
	for (;;) {
		while (dev->eq_cons != smp_load_acquire(&sh->eq_prod)) {
			struct shmrdma_eqe *e =
				&dev->eq[dev->eq_cons & (SHMRDMA_EQ_ENTRIES - 1)];
			u32 cqn = READ_ONCE(e->cqn);
			u32 type = READ_ONCE(e->type);
			struct shmrdma_cq *cq;

			dev->eq_cons++;
			WRITE_ONCE(sh->eq_cons, dev->eq_cons);
			if (type == SHMRDMA_EQE_QP_FATAL) {
				dev_dbg(&dev->vdev->dev, "qp %u fatal\n", cqn);
				continue;
			}
			cq = xa_load(&dev->cqs, cqn);
			if (cq && cq->ibcq.comp_handler)
				cq->ibcq.comp_handler(&cq->ibcq,
						      cq->ibcq.cq_context);
		}
		WRITE_ONCE(sh->eq_armed, 1);
		smp_mb();
		if (dev->eq_cons == READ_ONCE(sh->eq_prod))
			break;
	}
out:
	spin_unlock_irqrestore(&dev->eq_lock, flags);
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

static int shmrdma_query_port(struct ib_device *ibdev, u32 port,
			      struct ib_port_attr *attr)
{
	struct shmrdma_dev *dev = to_sdev(ibdev);
	bool up;

	if (port != 1)
		return -EINVAL;
	up = netif_running(dev->ndev) && netif_carrier_ok(dev->ndev);
	attr->state = up ? IB_PORT_ACTIVE : IB_PORT_DOWN;
	attr->phys_state = up ? IB_PORT_PHYS_STATE_LINK_UP :
				IB_PORT_PHYS_STATE_DISABLED;
	attr->max_mtu = IB_MTU_4096;
	attr->active_mtu = IB_MTU_4096;
	attr->gid_tbl_len = SHMRDMA_MAX_GID;
	attr->port_cap_flags = IB_PORT_CM_SUP;
	attr->max_msg_sz = 0x80000000;
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
	return udata ? -EOPNOTSUPP : 0;
}

static int shmrdma_dealloc_pd(struct ib_pd *pd, struct ib_udata *udata)
{
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

	if (attr->flags || attr->cqe < 1 || attr->cqe > SHMRDMA_MAX_CQE)
		return -EINVAL;
	entries = roundup_pow_of_two(attr->cqe + 1);
	rc = ida_alloc_range(&dev->cqn_ida, 0, SHMRDMA_MAX_CQ - 1,
			     GFP_KERNEL);
	if (rc < 0)
		return rc;
	cq->cqn = rc;
	spin_lock_init(&cq->lock);
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
	xa_erase_irq(&dev->cqs, cq->cqn);
	spin_unlock_irqrestore(&dev->eq_lock, flags);
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

	cmd.destroy_cq.cqn = cq->cqn;
	shmrdma_cmd(dev, &cmd, NULL);
	/* no completion handler may run after this */
	spin_lock_irqsave(&dev->eq_lock, flags);
	xa_erase(&dev->cqs, cq->cqn);
	spin_unlock_irqrestore(&dev->eq_lock, flags);
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

static int shmrdma_poll_cq(struct ib_cq *ibcq, int num, struct ib_wc *wc)
{
	struct shmrdma_dev *dev = to_sdev(ibcq->device);
	struct shmrdma_cq *cq = to_scq(ibcq);
	unsigned long flags;
	int n = 0;

	spin_lock_irqsave(&cq->lock, flags);
	while (n < num && cq->cons != smp_load_acquire(&cq->rec->prod)) {
		struct shmrdma_cqe *e = ring_at(&cq->ring, cq->cons);
		struct shmrdma_qp *qp;
		struct ib_wc *w = &wc[n];

		cq->cons++;
		qp = xa_load(&dev->qps, e->qpn);
		if (!qp)
			continue;
		memset(w, 0, sizeof(*w));
		w->wr_id = e->wr_id;
		w->status = e->status;
		w->opcode = e->opcode < ARRAY_SIZE(wc_opcode) ?
			    wc_opcode[e->opcode] : IB_WC_SEND;
		w->vendor_err = e->vendor_err;
		w->byte_len = e->byte_len;
		w->qp = &qp->ibqp;
		w->src_qp = e->src_qp;
		w->port_num = 1;
		if (e->flags & SHMRDMA_CQE_IMM) {
			w->wc_flags |= IB_WC_WITH_IMM;
			w->ex.imm_data = (__force __be32)e->ex;
		}
		if (e->flags & SHMRDMA_CQE_INV) {
			w->wc_flags |= IB_WC_WITH_INVALIDATE;
			w->ex.invalidate_rkey = e->ex;
		}
		if (e->flags & SHMRDMA_CQE_GRH) {
			w->wc_flags |= IB_WC_GRH | IB_WC_WITH_NETWORK_HDR_TYPE;
			w->network_hdr_type = (e->flags & SHMRDMA_CQE_IPV4) ?
				RDMA_NETWORK_IPV4 : RDMA_NETWORK_IPV6;
		}
		n++;
	}
	smp_store_release(&cq->rec->cons, cq->cons);
	spin_unlock_irqrestore(&cq->lock, flags);
	return n;
}

static int shmrdma_req_notify_cq(struct ib_cq *ibcq,
				 enum ib_cq_notify_flags flags)
{
	struct shmrdma_cq *cq = to_scq(ibcq);
	u32 arm = (flags & IB_CQ_SOLICITED_MASK) == IB_CQ_SOLICITED ?
		  SHMRDMA_CQ_ARM_SOLICITED : SHMRDMA_CQ_ARM_NEXT;

	if (arm == SHMRDMA_CQ_ARM_SOLICITED &&
	    READ_ONCE(cq->rec->arm) == SHMRDMA_CQ_ARM_NEXT)
		arm = SHMRDMA_CQ_ARM_NEXT;
	WRITE_ONCE(cq->rec->arm, arm);
	smp_mb();
	if ((flags & IB_CQ_REPORT_MISSED_EVENTS) &&
	    READ_ONCE(cq->cons) != READ_ONCE(cq->rec->prod))
		return 1;
	return 0;
}

/* ---------------------------------------------------------------- */
/* queue pairs                                                      */

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
		rc = ida_alloc_range(&dev->qpn_ida, 2, SHMRDMA_MAX_QP - 1,
				     GFP_KERNEL);
		if (rc < 0)
			return rc;
		qpn = rc;
	}
	qp->ibqp.qp_num = qpn;
	spin_lock_init(&qp->sq_lock);
	spin_lock_init(&qp->rq_lock);
	mutex_init(&qp->state_lock);
	qp->sig_all = init->sq_sig_type == IB_SIGNAL_ALL_WR;
	qp->max_send_sge = (sq_esz - SHMRDMA_WQE_HDR) / 16;
	if (qp->max_send_sge > SHMRDMA_MAX_SGE)
		qp->max_send_sge = SHMRDMA_MAX_SGE;
	qp->max_recv_sge = (rq_esz - sizeof(struct shmrdma_rq_wqe)) / 16;
	if (qp->max_recv_sge > SHMRDMA_MAX_SGE)
		qp->max_recv_sge = SHMRDMA_MAX_SGE;
	qp->max_inline = sq_esz - SHMRDMA_WQE_HDR;
	qp->attr.qp_state = IB_QPS_RESET;

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
	cmd.create_qp.send_cqn = to_scq(init->send_cq)->cqn;
	cmd.create_qp.recv_cqn = to_scq(init->recv_cq)->cqn;
	cmd.create_qp.rec_gpa = virt_to_phys(qp->rec);
	ring_desc(&qp->sq, &cmd.create_qp.sq);
	ring_desc(&qp->rq, &cmd.create_qp.rq);
	rc = xa_err(xa_store_irq(&dev->qps, qpn, qp, GFP_KERNEL));
	if (rc)
		goto err_rq;
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
	xa_erase_irq(&dev->qps, qpn);
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
	cur = (mask & IB_QP_CUR_STATE) ? attr->cur_qp_state :
					qp->attr.qp_state;
	next = (mask & IB_QP_STATE) ? attr->qp_state : cur;
	if (!ib_modify_qp_is_ok(cur, next, ibqp->qp_type, mask)) {
		rc = -EINVAL;
		goto out;
	}

	cmd.modify_qp.qpn = ibqp->qp_num;
	if (mask & IB_QP_STATE) {
		cmd.modify_qp.mask |= SHMRDMA_MQ_STATE;
		cmd.modify_qp.state = next;
	}
	if (mask & IB_QP_AV) {
		const struct ib_global_route *grh = rdma_ah_read_grh(&attr->ah_attr);

		memcpy(qp->dgid, grh->dgid.raw, 16);
	}
	if (mask & IB_QP_DEST_QPN)
		qp->attr.dest_qp_num = attr->dest_qp_num;
	if (mask & (IB_QP_AV | IB_QP_DEST_QPN)) {
		cmd.modify_qp.mask |= SHMRDMA_MQ_DEST;
		memcpy(cmd.modify_qp.dgid, qp->dgid, 16);
		cmd.modify_qp.dest_qpn = qp->attr.dest_qp_num;
	}
	if (mask & IB_QP_QKEY) {
		cmd.modify_qp.mask |= SHMRDMA_MQ_QKEY;
		cmd.modify_qp.qkey = attr->qkey;
		qp->attr.qkey = attr->qkey;
	}
	if (mask & IB_QP_RNR_RETRY) {
		cmd.modify_qp.mask |= SHMRDMA_MQ_RETRY;
		cmd.modify_qp.rnr_retry = attr->rnr_retry;
		qp->attr.rnr_retry = attr->rnr_retry;
	}
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
	if (mask & IB_QP_ACCESS_FLAGS)
		qp->attr.qp_access_flags = attr->qp_access_flags;
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

	if (cmd.modify_qp.mask) {
		rc = shmrdma_cmd(dev, &cmd, NULL);
		if (rc)
			goto out;
	}
	if (mask & IB_QP_STATE) {
		qp->attr.qp_state = next;
		qp->attr.cur_qp_state = next;
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

static int shmrdma_destroy_qp(struct ib_qp *ibqp, struct ib_udata *udata)
{
	struct shmrdma_dev *dev = to_sdev(ibqp->device);
	struct shmrdma_qp *qp = to_sqp(ibqp);
	struct shmrdma_cmd cmd = { .cmd = SHMRDMA_CMD_DESTROY_QP };

	cmd.destroy_qp.qpn = ibqp->qp_num;
	shmrdma_cmd(dev, &cmd, NULL);
	xa_erase_irq(&dev->qps, ibqp->qp_num);
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
			if (len + wr->sg_list[i].length > qp->max_inline)
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

	if (st != IB_QPS_RTS && st != IB_QPS_ERR && st != IB_QPS_SQD &&
	    st != IB_QPS_SQE) {
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
	shmrdma_doorbell(dev, qp->rec);
	if (rc)
		*bad = wr;
	return rc;
}

/* ---------------------------------------------------------------- */
/* memory regions                                                   */

static struct ib_mr *shmrdma_get_dma_mr(struct ib_pd *pd, int access)
{
	struct shmrdma_mr *mr = kzalloc(sizeof(*mr), GFP_KERNEL);

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
	rc = ida_alloc_range(&dev->mr_ida, 1, SHMRDMA_MAX_MR - 1, GFP_KERNEL);
	if (rc < 0) {
		kfree(mr->pages);
		kfree(mr);
		return ERR_PTR(rc);
	}
	mr->index = rc;
	mr->max_pages = max_num_sg;
	mr->ibmr.lkey = mr->index << 8;
	mr->ibmr.rkey = mr->index << 8;
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

	if (mr->index) {
		struct shmrdma_cmd cmd = { .cmd = SHMRDMA_CMD_DEREG_MR };

		cmd.dereg_mr.key = ibmr->lkey;
		shmrdma_cmd(dev, &cmd, NULL);
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
	a->page_size_cap = 0xfffff000;
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
	a->max_pd = 1 << 20;
	a->max_qp_rd_atom = 16;
	a->max_res_rd_atom = 16 * (SHMRDMA_MAX_QP - 2);
	a->max_qp_init_rd_atom = 16;
	a->atomic_cap = IB_ATOMIC_NONE;
	a->max_ah = 1 << 20;
	a->max_fast_reg_page_list_len = SHMRDMA_MAX_FRPL;
	a->max_pkeys = 1;
	a->local_ca_ack_delay = 15;
	addrconf_addr_eui48((unsigned char *)&a->sys_image_guid,
			    dev->ndev->dev_addr);
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
	struct shmrdma_dev *dev;
	struct net_device *ndev;
	struct scatterlist sg;
	u64 val;
	int rc, i;

	char tag[sizeof_field(struct virtio_fs_config, tag) + 1] = "";

	virtio_cread_bytes(vdev, offsetof(struct virtio_fs_config, tag), tag,
			   sizeof(tag) - 1);
	if (strcmp(tag, SHMRDMA_TAG))
		return -ENODEV;

	ndev = dev_get_by_name(&init_net, netdev);
	if (!ndev) {
		dev_err(&vdev->dev, "no netdev %s\n", netdev);
		return -ENODEV;
	}
	dev = ib_alloc_device(shmrdma_dev, ibdev);
	if (!dev) {
		dev_put(ndev);
		return -ENOMEM;
	}
	dev->vdev = vdev;
	dev->ndev = ndev;
	vdev->priv = dev;
	mutex_init(&dev->ctrl_mutex);
	init_completion(&dev->ctrl_done);
	spin_lock_init(&dev->eq_lock);
	xa_init_flags(&dev->qps, XA_FLAGS_LOCK_IRQ);
	xa_init_flags(&dev->cqs, XA_FLAGS_LOCK_IRQ);
	ida_init(&dev->qpn_ida);
	ida_init(&dev->cqn_ida);
	ida_init(&dev->mr_ida);

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
	rc = shmrdma_cmd(dev, &cmd, &val);
	if (rc) {
		dev_err(&vdev->dev, "backend init failed: %d\n", rc);
		goto err_reset;
	}

	shmrdma_init_attr(dev);
	strscpy(dev->ibdev.node_desc, "shmrdma", sizeof(dev->ibdev.node_desc));
	dev->ibdev.node_type = RDMA_NODE_IB_CA;
	dev->ibdev.phys_port_cnt = 1;
	dev->ibdev.num_comp_vectors = 1;
	dev->ibdev.local_dma_lkey = SHMRDMA_LOCAL_DMA_LKEY;
	addrconf_addr_eui48((unsigned char *)&dev->ibdev.node_guid,
			    ndev->dev_addr);
	ib_set_device_ops(&dev->ibdev, &shmrdma_dev_ops);
	rc = ib_device_set_netdev(&dev->ibdev, ndev, 1);
	if (rc)
		goto err_reset;
	rc = ib_register_device(&dev->ibdev, "shmrdma%d", vdev->dev.parent);
	if (rc) {
		dev_err(&vdev->dev, "ib_register_device: %d\n", rc);
		goto err_reset;
	}
	dev->registered = true;
	dev_info(&vdev->dev, "%s on %s, backend dev%llu\n",
		 dev_name(&dev->ibdev.dev), netdev, val);
	return 0;

err_reset:
	virtio_reset_device(vdev);
	vdev->config->del_vqs(vdev);
err_free:
	if (dev->eq)
		free_pages_exact(dev->eq, SHMRDMA_EQ_ENTRIES *
				 sizeof(struct shmrdma_eqe));
	free_page((unsigned long)dev->shared);
	kfree(dev->evbufs);
	kfree(dev->respbuf);
	kfree(dev->cmdbuf);
	dev_put(ndev);
	ib_dealloc_device(&dev->ibdev);
	return rc;
}

static void shmrdma_remove(struct virtio_device *vdev)
{
	struct shmrdma_dev *dev = vdev->priv;
	struct shmrdma_cmd cmd = { .cmd = SHMRDMA_CMD_RESET };

	if (dev->registered)
		ib_unregister_device(&dev->ibdev);
	shmrdma_cmd(dev, &cmd, NULL);
	virtio_reset_device(vdev);
	vdev->config->del_vqs(vdev);
	free_pages_exact(dev->eq, SHMRDMA_EQ_ENTRIES *
			 sizeof(struct shmrdma_eqe));
	free_page((unsigned long)dev->shared);
	kfree(dev->evbufs);
	kfree(dev->respbuf);
	kfree(dev->cmdbuf);
	dev_put(dev->ndev);
	ib_dealloc_device(&dev->ibdev);
}

static const struct virtio_device_id id_table[] = {
	{ SHMRDMA_VIRTIO_ID, VIRTIO_DEV_ANY_ID },
	{ 0 },
};

static unsigned int features[] = { };

static struct virtio_driver shmrdma_driver = {
	.feature_table = features,
	.feature_table_size = ARRAY_SIZE(features),
	.driver.name = KBUILD_MODNAME,
	.id_table = id_table,
	.probe = shmrdma_probe,
	.remove = shmrdma_remove,
};

module_virtio_driver(shmrdma_driver);
MODULE_DEVICE_TABLE(virtio, id_table);
MODULE_DESCRIPTION("RDMA between VMs through host shared memory");
MODULE_LICENSE("GPL");
