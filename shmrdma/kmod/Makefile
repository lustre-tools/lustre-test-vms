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

KDIR ?= /lib/modules/$(shell uname -r)/build

all:
	$(MAKE) -C $(KDIR) M=$(CURDIR) modules

clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) clean
