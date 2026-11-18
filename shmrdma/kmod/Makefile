obj-m := shmrdma.o
ccflags-y := -I$(src)/../include
# reg_user_mr gained a struct ib_dmah argument (6.17, backported to EL10.2)
ifneq ($(shell grep -s "struct ib_dmah \*dmah" $(srctree)/include/rdma/ib_verbs.h),)
ccflags-y += -DHAVE_IB_DMAH
endif
# create_cq takes a uverbs_attr_bundle since 6.8 (EL9.5)
ifneq ($(shell grep -s -A1 "(\*create_cq)" $(srctree)/include/rdma/ib_verbs.h | grep uverbs_attr_bundle),)
ccflags-y += -DHAVE_CREATE_CQ_ATTRS
endif
# virtio_find_vqs takes struct virtqueue_info since 6.11
ifneq ($(shell grep -s "struct virtqueue_info {" $(srctree)/include/linux/virtio_config.h),)
ccflags-y += -DHAVE_VIRTQUEUE_INFO
endif
# virtio drivers got a shutdown method with the core's shutdown reset
ifneq ($(shell grep -s "void (\*shutdown)(struct virtio_device \*dev);" $(srctree)/include/linux/virtio.h),)
ccflags-y += -DHAVE_VIRTIO_DRIVER_SHUTDOWN
endif
# virtio_reset_device replaced config->reset in 5.16 (not in EL8)
ifneq ($(shell grep -s "void virtio_reset_device" $(srctree)/include/linux/virtio.h),)
ccflags-y += -DHAVE_VIRTIO_RESET_DEVICE
endif

# ib_core sends port events from the netdev's state itself
ifneq ($(shell grep -s "void ib_dispatch_port_state_event" $(srctree)/include/rdma/ib_verbs.h),)
ccflags-y += -DHAVE_IB_DISPATCH_PORT_STATE_EVENT
endif

# ib_umem_get took the udata before 5.5 (EL8), and became ib_umem_get_va in 7.3
ifneq ($(shell grep -s "ib_umem_get_va" $(srctree)/include/rdma/ib_umem.h),)
ccflags-y += -DHAVE_IB_UMEM_GET_VA
else ifneq ($(shell grep -s "ib_umem_get.struct ib_udata" $(srctree)/include/rdma/ib_umem.h),)
ccflags-y += -DHAVE_IB_UMEM_GET_UDATA
endif

KDIR ?= /lib/modules/$(shell uname -r)/build

all:
	$(MAKE) -C $(KDIR) M=$(CURDIR) modules

clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) clean
