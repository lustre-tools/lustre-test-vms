CFLAGS ?= -O2 -g -Wall -Wextra -Wno-unused-parameter -Wno-unused-result
CFLAGS += -pthread -std=gnu11

all: shmrdmad

shmrdmad: shmrdmad.c shmnet.c fault.c plat.h plat_linux.h plat_macos.h ../include/shmrdma_abi.h
	$(CC) $(CFLAGS) -o $@ shmrdmad.c

.PHONY: all clean

clean:
	rm -f shmrdmad
