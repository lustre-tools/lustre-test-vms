// SPDX-License-Identifier: GPL-2.0 OR BSD-2-Clause
/*
 * libibverbs provider for shmrdma, the RDMA device ltvm's VMs share
 * through host memory (shmrdma.ko in the guest, shmrdmad on the host).
 *
 * A process's CQs and QPs live in buffers it allocates here; the driver
 * pins them and hands their pages to the host daemon, which reads the
 * work queues and writes completions straight into them.  Posting and
 * polling need no system call, except a doorbell when the daemon has
 * gone idle on a QP (its record's need_kick), which is a post_send
 * command with no work requests.
 *
 * Built inside an rdma-core source tree of the version installed in
 * the image (build-provider.sh), since a provider links
 * against libibverbs' private ABI.
 */
#include <config.h>

#include <endian.h>
#include <errno.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

#include <infiniband/driver.h>
#include <infiniband/verbs.h>

#include "shmrdma-abi.h"

struct shm_device {
	struct verbs_device ibv_dev;
};

struct shm_context {
	struct verbs_context ibv_ctx;
	pthread_mutex_t qp_lock;
	struct shm_qp *qps[SHMRDMA_MAX_QP];
};

struct shm_cq {
	struct ibv_cq ibv_cq;
	pthread_spinlock_t lock;
	void *buf;
	size_t len;
	struct shmrdma_cq_rec *rec;
	struct shmrdma_cqe *ring;
	uint32_t entries;
	uint32_t cons;
};

struct shm_qp {
	struct ibv_qp ibv_qp;
	void *buf;
	size_t len;
	struct shmrdma_qp_rec *rec;
	uint8_t *sq, *rq;
	struct shmrdma_qp_geom g;
	uint32_t type;
	int sig_all;
	enum ibv_qp_state state;
	pthread_spinlock_t sq_lock, rq_lock;
	uint32_t sq_prod, rq_prod;
};

struct shm_ah {
	struct ibv_ah ibv_ah;
	uint8_t dgid[16];
	uint8_t sgid[16];
};

#define to_sctx(c) container_of(c, struct shm_context, ibv_ctx.context)
#define to_scq(c) container_of(c, struct shm_cq, ibv_cq)
#define to_sqp(q) container_of(q, struct shm_qp, ibv_qp)
#define to_sah(a) container_of(a, struct shm_ah, ibv_ah)

static const struct verbs_match_ent hca_table[] = {
	VERBS_NAME_MATCH("shmrdma", NULL),
	{},
};

/* ---- shared memory with the daemon ---- */

#define LOAD_ACQ(p)	__atomic_load_n((p), __ATOMIC_ACQUIRE)
#define STORE_REL(p, v)	__atomic_store_n((p), (v), __ATOMIC_RELEASE)
#define LOAD(p)		__atomic_load_n((p), __ATOMIC_RELAXED)
#define STORE(p, v)	__atomic_store_n((p), (v), __ATOMIC_RELAXED)

static void *buf_alloc(size_t *len)
{
	size_t pg = sysconf(_SC_PAGESIZE);
	void *p;

	*len = (*len + pg - 1) & ~(pg - 1);
	p = mmap(NULL, *len, PROT_READ | PROT_WRITE,
		 MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (p == MAP_FAILED)
		return NULL;
	if (ibv_dontfork_range(p, *len)) {
		munmap(p, *len);
		return NULL;
	}
	return p;
}

static void buf_free(void *p, size_t len)
{
	ibv_dofork_range(p, len);
	munmap(p, len);
}

/*
 * The daemon went idle on this QP: wake it through the driver.  Work
 * already published stays posted if this fails; the next doorbell
 * delivers it.
 */
static void doorbell(struct shm_qp *qp)
{
	struct ibv_post_send cmd;
	struct ib_uverbs_post_send_resp resp;

	__atomic_thread_fence(__ATOMIC_SEQ_CST);
	/* after a reset, the driver flushes what was posted */
	if (!LOAD(&qp->rec->need_kick) && !LOAD(&qp->rec->fatal))
		return;
	memset(&cmd, 0, sizeof(cmd));
	cmd.hdr.command = IB_USER_VERBS_CMD_POST_SEND;
	cmd.hdr.in_words = sizeof(cmd) / 4;
	cmd.hdr.out_words = sizeof(resp) / 4;
	cmd.response = (uintptr_t)&resp;
	cmd.qp_handle = qp->ibv_qp.handle;
	cmd.wr_count = 0;
	cmd.sge_count = 0;
	cmd.wqe_size = sizeof(struct ibv_send_wr);
	if (write(qp->ibv_qp.context->cmd_fd, &cmd, sizeof(cmd)) != sizeof(cmd))
		verbs_warn(verbs_get_ctx(qp->ibv_qp.context),
			   "shmrdma: doorbell failed: %s\n", strerror(errno));
}

/* ---- device, PD ---- */

static int shm_query_device(struct ibv_context *context,
			    const struct ibv_query_device_ex_input *input,
			    struct ibv_device_attr_ex *attr, size_t attr_size)
{
	struct ib_uverbs_ex_query_device_resp resp;
	size_t resp_size = sizeof(resp);
	int ret;

	ret = ibv_cmd_query_device_any(context, input, attr, attr_size, &resp,
				       &resp_size);
	if (ret)
		return ret;
	snprintf(attr->orig_attr.fw_ver, sizeof(attr->orig_attr.fw_ver),
		 "abi %u", SHMRDMA_UABI_VERSION);
	return 0;
}

static int shm_query_port(struct ibv_context *context, uint8_t port,
			  struct ibv_port_attr *attr)
{
	struct ibv_query_port cmd;

	return ibv_cmd_query_port(context, port, attr, &cmd, sizeof(cmd));
}

static struct ibv_pd *shm_alloc_pd(struct ibv_context *context)
{
	struct ibv_alloc_pd cmd;
	struct ib_uverbs_alloc_pd_resp resp;
	struct ibv_pd *pd;

	pd = calloc(1, sizeof(*pd));
	if (!pd)
		return NULL;
	if (ibv_cmd_alloc_pd(context, pd, &cmd, sizeof(cmd), &resp,
			     sizeof(resp))) {
		free(pd);
		return NULL;
	}
	return pd;
}

static int shm_dealloc_pd(struct ibv_pd *pd)
{
	int ret = ibv_cmd_dealloc_pd(pd);

	if (!ret)
		free(pd);
	return ret;
}

/* ---- MRs ---- */

static struct ibv_mr *shm_reg_mr(struct ibv_pd *pd, void *addr, size_t length,
				 uint64_t hca_va, int access)
{
	struct ibv_reg_mr cmd;
	struct ib_uverbs_reg_mr_resp resp;
	struct verbs_mr *vmr;

	vmr = calloc(1, sizeof(*vmr));
	if (!vmr)
		return NULL;
	if (ibv_cmd_reg_mr(pd, addr, length, hca_va, access, vmr, &cmd,
			   sizeof(cmd), &resp, sizeof(resp))) {
		free(vmr);
		return NULL;
	}
	return &vmr->ibv_mr;
}

static int shm_dereg_mr(struct verbs_mr *vmr)
{
	int ret = ibv_cmd_dereg_mr(vmr);

	if (!ret)
		free(vmr);
	return ret;
}

/* ---- CQs ---- */

static struct ibv_cq *shm_create_cq(struct ibv_context *context, int cqe,
				    struct ibv_comp_channel *channel,
				    int comp_vector)
{
	struct ushm_create_cq cmd = {};
	struct ushm_create_cq_resp resp = {};
	struct shm_cq *cq;
	uint32_t entries;

	if (cqe < 1 || cqe >= SHMRDMA_MAX_CQE) {
		errno = EINVAL;
		return NULL;
	}
	entries = shmrdma_pow2(cqe + 1);
	cq = calloc(1, sizeof(*cq));
	if (!cq)
		return NULL;
	cq->len = shmrdma_cq_len(entries);
	cq->buf = buf_alloc(&cq->len);
	if (!cq->buf)
		goto err_cq;
	cq->rec = cq->buf;
	cq->ring = (void *)((uint8_t *)cq->buf + SHMRDMA_RING_PAGE_SIZE);
	cq->entries = entries;
	pthread_spin_init(&cq->lock, PTHREAD_PROCESS_PRIVATE);
	cmd.buf = (uintptr_t)cq->buf;
	cmd.len = cq->len;
	if (ibv_cmd_create_cq(context, cqe, channel, comp_vector, &cq->ibv_cq,
			      &cmd.ibv_cmd, sizeof(cmd), &resp.ibv_resp,
			      sizeof(resp)))
		goto err_buf;
	if (resp.entries != entries) {
		ibv_cmd_destroy_cq(&cq->ibv_cq);
		errno = EPROTO;
		goto err_buf;
	}
	return &cq->ibv_cq;

err_buf:
	pthread_spin_destroy(&cq->lock);
	if (cq->buf)
		buf_free(cq->buf, cq->len);
err_cq:
	free(cq);
	return NULL;
}

static int shm_destroy_cq(struct ibv_cq *ibcq)
{
	struct shm_cq *cq = to_scq(ibcq);
	int ret = ibv_cmd_destroy_cq(ibcq);

	if (ret)
		return ret;
	pthread_spin_destroy(&cq->lock);
	buf_free(cq->buf, cq->len);
	free(cq);
	return 0;
}

static const enum ibv_wc_opcode wc_opcode[] = {
	[SHMRDMA_WC_SEND] = IBV_WC_SEND,
	[SHMRDMA_WC_RDMA_WRITE] = IBV_WC_RDMA_WRITE,
	[SHMRDMA_WC_RDMA_READ] = IBV_WC_RDMA_READ,
	[SHMRDMA_WC_REG_MR] = IBV_WC_BIND_MW,
	[SHMRDMA_WC_LOCAL_INV] = IBV_WC_LOCAL_INV,
	[SHMRDMA_WC_RECV] = IBV_WC_RECV,
	[SHMRDMA_WC_RECV_RDMA_IMM] = IBV_WC_RECV_RDMA_WITH_IMM,
};

static int shm_poll_cq(struct ibv_cq *ibcq, int ne, struct ibv_wc *wc)
{
	struct shm_cq *cq = to_scq(ibcq);
	struct shm_context *ctx = to_sctx(ibcq->context);
	uint32_t prod;
	int n = 0;

	pthread_spin_lock(&cq->lock);
	prod = LOAD_ACQ(&cq->rec->prod);
	/* a producer past the ring is the daemon's fault: report nothing */
	if (prod - cq->cons > cq->entries)
		prod = cq->cons;
	while (n < ne && cq->cons != prod) {
		struct shmrdma_cqe c;
		struct ibv_wc *w = &wc[n];

		memcpy(&c, &cq->ring[cq->cons & (cq->entries - 1)], sizeof(c));
		cq->cons++;
		if (c.qpn >= SHMRDMA_MAX_QP || !LOAD(&ctx->qps[c.qpn]))
			continue;
		memset(w, 0, sizeof(*w));
		w->wr_id = c.wr_id;
		w->status = c.status;
		w->opcode = c.opcode < sizeof(wc_opcode) / sizeof(wc_opcode[0]) ?
			    wc_opcode[c.opcode] : IBV_WC_SEND;
		w->vendor_err = c.vendor_err;
		w->byte_len = c.byte_len < SHMRDMA_MAX_MSG ? c.byte_len :
							     SHMRDMA_MAX_MSG;
		w->qp_num = c.qpn;
		w->src_qp = c.src_qp;
		if (c.flags & SHMRDMA_CQE_IMM) {
			w->wc_flags |= IBV_WC_WITH_IMM;
			w->imm_data = c.ex;	/* big endian as sent */
		}
		if (c.flags & SHMRDMA_CQE_INV) {
			w->wc_flags |= IBV_WC_WITH_INV;
			w->invalidated_rkey = c.ex;
		}
		if (c.flags & SHMRDMA_CQE_GRH)
			w->wc_flags |= IBV_WC_GRH;
		n++;
	}
	STORE_REL(&cq->rec->cons, cq->cons);
	pthread_spin_unlock(&cq->lock);
	/* after a reset the CQ filled with flushes: ask for the rest */
	__atomic_thread_fence(__ATOMIC_SEQ_CST);
	if (LOAD(&cq->rec->flush))
		ibv_cmd_req_notify_cq(ibcq, 0);
	return n;
}

static int shm_req_notify_cq(struct ibv_cq *ibcq, int solicited_only)
{
	struct shm_cq *cq = to_scq(ibcq);
	uint32_t none = SHMRDMA_CQ_ARM_NONE;

	/* solicited only never narrows an arm for any completion */
	if (solicited_only)
		__atomic_compare_exchange_n(&cq->rec->arm, &none,
					    SHMRDMA_CQ_ARM_SOLICITED, false,
					    __ATOMIC_RELAXED, __ATOMIC_RELAXED);
	else
		STORE(&cq->rec->arm, SHMRDMA_CQ_ARM_NEXT);
	__atomic_thread_fence(__ATOMIC_SEQ_CST);
	return 0;
}

/* ---- QPs ---- */

static struct ibv_qp *shm_create_qp(struct ibv_pd *pd,
				    struct ibv_qp_init_attr *attr)
{
	struct shm_context *ctx = to_sctx(pd->context);
	struct ushm_create_qp cmd = {};
	struct ushm_create_qp_resp resp = {};
	struct ibv_qp_cap *cap = &attr->cap;
	struct shm_qp *qp;
	uint32_t type;

	switch (attr->qp_type) {
	case IBV_QPT_RC:
		type = SHMRDMA_QPT_RC;
		break;
	case IBV_QPT_UD:
		type = SHMRDMA_QPT_UD;
		break;
	default:
		errno = EOPNOTSUPP;
		return NULL;
	}
	if (attr->srq) {
		errno = EOPNOTSUPP;
		return NULL;
	}
	qp = calloc(1, sizeof(*qp));
	if (!qp)
		return NULL;
	if (shmrdma_qp_geom(type, cap->max_send_wr, cap->max_recv_wr,
			    cap->max_send_sge, cap->max_recv_sge,
			    cap->max_inline_data, &qp->g)) {
		errno = EINVAL;
		goto err_qp;
	}
	qp->type = type;
	qp->sig_all = attr->sq_sig_all;
	qp->state = IBV_QPS_RESET;
	qp->len = qp->g.len;
	qp->buf = buf_alloc(&qp->len);
	if (!qp->buf)
		goto err_qp;
	qp->rec = qp->buf;
	qp->sq = (uint8_t *)qp->buf + qp->g.sq_off;
	qp->rq = (uint8_t *)qp->buf + qp->g.rq_off;
	pthread_spin_init(&qp->sq_lock, PTHREAD_PROCESS_PRIVATE);
	pthread_spin_init(&qp->rq_lock, PTHREAD_PROCESS_PRIVATE);
	cmd.buf = (uintptr_t)qp->buf;
	cmd.len = qp->len;
	if (ibv_cmd_create_qp(pd, &qp->ibv_qp, attr, &cmd.ibv_cmd, sizeof(cmd),
			      &resp.ibv_resp, sizeof(resp)))
		goto err_buf;
	if (qp->ibv_qp.qp_num >= SHMRDMA_MAX_QP) {
		ibv_cmd_destroy_qp(&qp->ibv_qp);
		errno = EPROTO;
		goto err_buf;
	}
	cap->max_send_wr = qp->g.sq_entries;
	cap->max_recv_wr = qp->g.rq_entries;
	cap->max_send_sge = qp->g.max_send_sge;
	cap->max_recv_sge = qp->g.max_recv_sge;
	cap->max_inline_data = qp->g.max_inline;
	pthread_mutex_lock(&ctx->qp_lock);
	STORE(&ctx->qps[qp->ibv_qp.qp_num], qp);
	pthread_mutex_unlock(&ctx->qp_lock);
	return &qp->ibv_qp;

err_buf:
	pthread_spin_destroy(&qp->sq_lock);
	pthread_spin_destroy(&qp->rq_lock);
	buf_free(qp->buf, qp->len);
err_qp:
	free(qp);
	return NULL;
}

static int shm_query_qp(struct ibv_qp *ibqp, struct ibv_qp_attr *attr,
			int attr_mask, struct ibv_qp_init_attr *init_attr)
{
	struct ibv_query_qp cmd;

	return ibv_cmd_query_qp(ibqp, attr, attr_mask, init_attr, &cmd,
				sizeof(cmd));
}

static int shm_modify_qp(struct ibv_qp *ibqp, struct ibv_qp_attr *attr,
			 int attr_mask)
{
	struct shm_qp *qp = to_sqp(ibqp);
	struct ibv_modify_qp cmd;
	int ret;

	ret = ibv_cmd_modify_qp(ibqp, attr, attr_mask, &cmd, sizeof(cmd));
	if (!ret && (attr_mask & IBV_QP_STATE))
		STORE(&qp->state, attr->qp_state);
	return ret;
}

static int shm_destroy_qp(struct ibv_qp *ibqp)
{
	struct shm_context *ctx = to_sctx(ibqp->context);
	struct shm_qp *qp = to_sqp(ibqp);
	uint32_t qpn = ibqp->qp_num;
	int ret;

	ret = ibv_cmd_destroy_qp(ibqp);
	if (ret)
		return ret;
	/* the number may already be a new QP's */
	pthread_mutex_lock(&ctx->qp_lock);
	if (ctx->qps[qpn] == qp)
		STORE(&ctx->qps[qpn], NULL);
	pthread_mutex_unlock(&ctx->qp_lock);
	pthread_spin_destroy(&qp->sq_lock);
	pthread_spin_destroy(&qp->rq_lock);
	buf_free(qp->buf, qp->len);
	free(qp);
	return 0;
}

/* the state the QP is in, as far as posting goes */
static enum ibv_qp_state qp_state(struct shm_qp *qp)
{
	enum ibv_qp_state st = LOAD(&qp->state);

	if (st != IBV_QPS_RESET &&
	    (LOAD(&qp->rec->fatal) || LOAD(&qp->rec->err)))
		return IBV_QPS_ERR;
	return st;
}

static int build_wqe(struct shm_qp *qp, struct ibv_send_wr *wr,
		     struct shmrdma_sq_wqe *w)
{
	int i;

	memset(w, 0, SHMRDMA_WQE_HDR);
	w->wr_id = wr->wr_id;
	if ((wr->send_flags & IBV_SEND_SIGNALED) || qp->sig_all)
		w->flags |= SHMRDMA_WQE_SIGNALED;
	if (wr->send_flags & IBV_SEND_SOLICITED)
		w->flags |= SHMRDMA_WQE_SOLICITED;
	if (wr->send_flags & IBV_SEND_FENCE)
		w->flags |= SHMRDMA_WQE_FENCE;

	switch (wr->opcode) {
	case IBV_WR_SEND:
		w->opcode = SHMRDMA_OP_SEND;
		break;
	case IBV_WR_SEND_WITH_IMM:
		w->opcode = SHMRDMA_OP_SEND_IMM;
		w->ex = wr->imm_data;
		break;
	case IBV_WR_SEND_WITH_INV:
		w->opcode = SHMRDMA_OP_SEND_INV;
		w->ex = wr->invalidate_rkey;
		break;
	case IBV_WR_RDMA_WRITE:
		w->opcode = SHMRDMA_OP_RDMA_WRITE;
		break;
	case IBV_WR_RDMA_WRITE_WITH_IMM:
		w->opcode = SHMRDMA_OP_RDMA_WRITE_IMM;
		w->ex = wr->imm_data;
		break;
	case IBV_WR_RDMA_READ:
		w->opcode = SHMRDMA_OP_RDMA_READ;
		break;
	case IBV_WR_LOCAL_INV:
		if (qp->type != SHMRDMA_QPT_RC)
			return EINVAL;
		w->opcode = SHMRDMA_OP_LOCAL_INV;
		w->ex = wr->invalidate_rkey;
		return 0;
	default:
		return EINVAL;
	}

	if (qp->type != SHMRDMA_QPT_RC) {
		struct shm_ah *ah;

		if (w->opcode != SHMRDMA_OP_SEND &&
		    w->opcode != SHMRDMA_OP_SEND_IMM)
			return EINVAL;
		if (!wr->wr.ud.ah)
			return EINVAL;
		ah = to_sah(wr->wr.ud.ah);
		w->ud.dqpn = wr->wr.ud.remote_qpn;
		w->ud.qkey = wr->wr.ud.remote_qkey;
		memcpy(w->ud.dgid, ah->dgid, 16);
		memcpy(w->ud.sgid, ah->sgid, 16);
	} else if (w->opcode == SHMRDMA_OP_RDMA_WRITE ||
		   w->opcode == SHMRDMA_OP_RDMA_WRITE_IMM ||
		   w->opcode == SHMRDMA_OP_RDMA_READ) {
		w->rdma.remote_addr = wr->wr.rdma.remote_addr;
		w->rdma.rkey = wr->wr.rdma.rkey;
	}

	if ((wr->send_flags & IBV_SEND_INLINE) &&
	    w->opcode != SHMRDMA_OP_RDMA_READ) {
		uint8_t *p = (uint8_t *)w->sge;
		uint32_t len = 0;

		/* a UD WQE's address fields overlap the inline length */
		if (qp->type != SHMRDMA_QPT_RC)
			return EINVAL;
		for (i = 0; i < wr->num_sge; i++) {
			if (wr->sg_list[i].length > qp->g.max_inline - len)
				return EINVAL;
			memcpy(p + len, (void *)(uintptr_t)wr->sg_list[i].addr,
			       wr->sg_list[i].length);
			len += wr->sg_list[i].length;
		}
		w->flags |= SHMRDMA_WQE_INLINE;
		if (w->opcode == SHMRDMA_OP_RDMA_WRITE ||
		    w->opcode == SHMRDMA_OP_RDMA_WRITE_IMM)
			w->rdma.inline_len = len;
		else
			w->send.inline_len = len;
		return 0;
	}
	if (wr->num_sge < 0 || (uint32_t)wr->num_sge > qp->g.max_send_sge)
		return EINVAL;
	w->num_sge = wr->num_sge;
	for (i = 0; i < wr->num_sge; i++) {
		w->sge[i].addr = wr->sg_list[i].addr;
		w->sge[i].length = wr->sg_list[i].length;
		w->sge[i].lkey = wr->sg_list[i].lkey;
	}
	return 0;
}

static int shm_post_send(struct ibv_qp *ibqp, struct ibv_send_wr *wr,
			 struct ibv_send_wr **bad)
{
	struct shm_qp *qp = to_sqp(ibqp);
	enum ibv_qp_state st = qp_state(qp);
	int rc = 0;

	if (st != IBV_QPS_RTS && st != IBV_QPS_ERR) {
		*bad = wr;
		return EINVAL;
	}
	pthread_spin_lock(&qp->sq_lock);
	for (; wr; wr = wr->next) {
		if (qp->sq_prod - LOAD_ACQ(&qp->rec->sq_cons) >=
		    qp->g.sq_entries) {
			rc = ENOMEM;
			break;
		}
		rc = build_wqe(qp, wr, (void *)(qp->sq +
				(size_t)(qp->sq_prod & (qp->g.sq_entries - 1)) *
				qp->g.sq_esz));
		if (rc)
			break;
		qp->sq_prod++;
	}
	STORE_REL(&qp->rec->sq_prod, qp->sq_prod);
	pthread_spin_unlock(&qp->sq_lock);
	doorbell(qp);
	if (rc)
		*bad = wr;
	return rc;
}

static int shm_post_recv(struct ibv_qp *ibqp, struct ibv_recv_wr *wr,
			 struct ibv_recv_wr **bad)
{
	struct shm_qp *qp = to_sqp(ibqp);
	int rc = 0, i;

	if (qp_state(qp) == IBV_QPS_RESET) {
		*bad = wr;
		return EINVAL;
	}
	pthread_spin_lock(&qp->rq_lock);
	for (; wr; wr = wr->next) {
		struct shmrdma_rq_wqe *w;

		if (qp->rq_prod - LOAD_ACQ(&qp->rec->rq_cons) >=
		    qp->g.rq_entries) {
			rc = ENOMEM;
			break;
		}
		if (wr->num_sge < 0 ||
		    (uint32_t)wr->num_sge > qp->g.max_recv_sge) {
			rc = EINVAL;
			break;
		}
		w = (void *)(qp->rq + (size_t)(qp->rq_prod &
					       (qp->g.rq_entries - 1)) *
				      qp->g.rq_esz);
		w->wr_id = wr->wr_id;
		w->num_sge = wr->num_sge;
		for (i = 0; i < wr->num_sge; i++) {
			w->sge[i].addr = wr->sg_list[i].addr;
			w->sge[i].length = wr->sg_list[i].length;
			w->sge[i].lkey = wr->sg_list[i].lkey;
		}
		qp->rq_prod++;
	}
	STORE_REL(&qp->rec->rq_prod, qp->rq_prod);
	pthread_spin_unlock(&qp->rq_lock);
	doorbell(qp);
	if (rc)
		*bad = wr;
	return rc;
}

/* ---- AHs ---- */

static struct ibv_ah *shm_create_ah(struct ibv_pd *pd, struct ibv_ah_attr *attr)
{
	struct ushm_create_ah_resp resp = {};
	struct shm_ah *ah;

	if (!attr->is_global) {
		errno = EINVAL;
		return NULL;
	}
	ah = calloc(1, sizeof(*ah));
	if (!ah)
		return NULL;
	if (ibv_cmd_create_ah(pd, &ah->ibv_ah, attr, &resp.ibv_resp,
			      sizeof(resp))) {
		free(ah);
		return NULL;
	}
	memcpy(ah->dgid, attr->grh.dgid.raw, 16);
	memcpy(ah->sgid, resp.sgid, 16);
	return &ah->ibv_ah;
}

static int shm_destroy_ah(struct ibv_ah *ibah)
{
	int ret = ibv_cmd_destroy_ah(ibah);

	if (!ret)
		free(to_sah(ibah));
	return ret;
}

/* ---- context and device ---- */

static void shm_free_context(struct ibv_context *ibctx);

static const struct verbs_context_ops shm_ctx_ops = {
	.query_device_ex = shm_query_device,
	.query_port = shm_query_port,
	.alloc_pd = shm_alloc_pd,
	.dealloc_pd = shm_dealloc_pd,
	.reg_mr = shm_reg_mr,
	.dereg_mr = shm_dereg_mr,
	.create_cq = shm_create_cq,
	.poll_cq = shm_poll_cq,
	.req_notify_cq = shm_req_notify_cq,
	.destroy_cq = shm_destroy_cq,
	.create_qp = shm_create_qp,
	.query_qp = shm_query_qp,
	.modify_qp = shm_modify_qp,
	.destroy_qp = shm_destroy_qp,
	.post_send = shm_post_send,
	.post_recv = shm_post_recv,
	.create_ah = shm_create_ah,
	.destroy_ah = shm_destroy_ah,
	.free_context = shm_free_context,
};

static struct verbs_context *shm_alloc_context(struct ibv_device *ibdev,
					       int cmd_fd, void *private_data)
{
	struct ushm_get_context cmd = {};
	struct ushm_get_context_resp resp = {};
	struct shm_context *ctx;

	ctx = verbs_init_and_alloc_context(ibdev, cmd_fd, ctx, ibv_ctx,
					   RDMA_DRIVER_UNKNOWN);
	if (!ctx)
		return NULL;
	if (ibv_cmd_get_context(&ctx->ibv_ctx, &cmd.ibv_cmd, sizeof(cmd),
#ifdef HAVE_GET_CONTEXT_FD_ARR
				NULL,
#endif
				&resp.ibv_resp, sizeof(resp)))
		goto err;
	if (resp.uabi != SHMRDMA_UABI_VERSION) {
		errno = EPROTO;
		goto err;
	}
	pthread_mutex_init(&ctx->qp_lock, NULL);
	verbs_set_ops(&ctx->ibv_ctx, &shm_ctx_ops);
	return &ctx->ibv_ctx;

err:
	verbs_uninit_context(&ctx->ibv_ctx);
	free(ctx);
	return NULL;
}

static void shm_free_context(struct ibv_context *ibctx)
{
	struct shm_context *ctx = to_sctx(ibctx);

	pthread_mutex_destroy(&ctx->qp_lock);
	verbs_uninit_context(&ctx->ibv_ctx);
	free(ctx);
}

static struct verbs_device *shm_device_alloc(struct verbs_sysfs_dev *sysfs_dev)
{
	struct shm_device *dev = calloc(1, sizeof(*dev));

	return dev ? &dev->ibv_dev : NULL;
}

static void shm_uninit_device(struct verbs_device *verbs_device)
{
	free(container_of(verbs_device, struct shm_device, ibv_dev));
}

static const struct verbs_device_ops shm_dev_ops = {
	.name = "shmrdma",
	.match_min_abi_version = 1,
	.match_max_abi_version = 1,
	.match_table = hca_table,
	.alloc_device = shm_device_alloc,
	.uninit_device = shm_uninit_device,
	.alloc_context = shm_alloc_context,
};
PROVIDER_DRIVER(shmrdma, shm_dev_ops);
