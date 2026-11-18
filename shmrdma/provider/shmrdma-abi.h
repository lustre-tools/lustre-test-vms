/* SPDX-License-Identifier: GPL-2.0 OR BSD-2-Clause */
/* The uverbs commands' driver data, for libibverbs' command macros. */
#ifndef SHMRDMA_UVERBS_ABI_H
#define SHMRDMA_UVERBS_ABI_H

#include <infiniband/kern-abi.h>
#include "shmrdma_abi.h"

#define _STRUCT_shmrdma_ucmd_buf struct { __aligned_u64 buf; __aligned_u64 len; }
#define _STRUCT_shmrdma_uresp_ctx struct { __u32 uabi; __u32 pad; }
#define _STRUCT_shmrdma_uresp_cq struct { __u32 cqn; __u32 entries; }
#define _STRUCT_shmrdma_uresp_ah struct { __u8 sgid[16]; }

DECLARE_DRV_CMD(ushm_get_context, IB_USER_VERBS_CMD_GET_CONTEXT,
		empty, shmrdma_uresp_ctx);
DECLARE_DRV_CMD(ushm_create_cq, IB_USER_VERBS_CMD_CREATE_CQ,
		shmrdma_ucmd_buf, shmrdma_uresp_cq);
DECLARE_DRV_CMD(ushm_create_qp, IB_USER_VERBS_CMD_CREATE_QP,
		shmrdma_ucmd_buf, empty);
DECLARE_DRV_CMD(ushm_create_ah, IB_USER_VERBS_CMD_CREATE_AH,
		empty, shmrdma_uresp_ah);

#endif
