/* SPDX-License-Identifier: GPL-2.0 */
/*
 * shmrdmad's platform layer: everything that differs between Linux and
 * macOS, or between x86_64 and aarch64.  shmrdmad.c and shmnet.c call
 * only this; they contain no platform code of their own.
 *
 * By OS, in plat_linux.h or plat_macos.h (one is included below):
 *   - poller: a set of fds that wakes one thread (epoll / kqueue);
 *   - notifiers: the kick and call fds QEMU passes, and each worker's
 *     wake fd.  An eventfd on Linux; on macOS QEMU uses non-blocking
 *     pipes (it writes an 8-byte 1 per kick and drops it when the pipe
 *     is full), so a woken reader must drain until EAGAIN.  One drain
 *     loop serves both: an eventfd empties in one read;
 *   - spinlocks, and the socket calls whose flags macOS lacks.
 *
 * By arch, here: now_ns, cpu_relax, the non-temporal bulk copy.
 *
 * Everything is static inline: the daemon is one translation unit
 * (ltvm builds it with a single cc call), and spinlocks and notifiers
 * sit on the hot path.
 */
#ifndef SHMRDMA_PLAT_H
#define SHMRDMA_PLAT_H

#include <errno.h>
#include <fcntl.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#if defined(__x86_64__)
#include <immintrin.h>
#endif

/* one event from plat_poller_wait() */
struct plat_event {
	void *tag;		/* what plat_poller_add() was given */
	bool hup;		/* the peer hung up, or the fd is in error */
};

#if defined(__linux__)
#include "plat_linux.h"
#elif defined(__APPLE__)
#include "plat_macos.h"
#else
#error "shmrdmad: no platform layer for this OS"
#endif

/*
 * Interface each OS file provides:
 *
 *   int  plat_poller_new(void);
 *   int  plat_poller_add(int poller, int fd, void *tag, bool edge);
 *		0, also when fd was already there
 *   void plat_poller_del(int poller, int fd);
 *   int  plat_poller_wait(int poller, struct plat_event *evs, int max,
 *			   int timeout_ms);
 *   int  plat_notifier_new(int *wfd);	read end; *wfd the write end
 *   plat_spinlock_t, plat_spin_init/lock/unlock/destroy
 *   int  plat_unix_socket(void);	AF_UNIX stream, close-on-exec
 *   int  plat_accept(int lsock);	close-on-exec
 *   ssize_t plat_recvmsg(int sock, struct msghdr *mh);
 *		received fds close-on-exec
 *   PLAT_MSG_NOSIGNAL			send() flag (SIGPIPE is ignored)
 */

/* wake whoever waits on the read end; EAGAIN means a wake is pending */
static inline void plat_notifier_signal(int wfd)
{
	static const uint64_t one = 1;

	(void)!write(wfd, &one, sizeof(one));
}

/* empty a notifier's read end (non-blocking) */
static inline void plat_notifier_drain(int rfd)
{
	uint64_t buf[64];
	ssize_t n;

	do {
		n = read(rfd, buf, sizeof(buf));
	} while (n == (ssize_t)sizeof(buf) || (n < 0 && errno == EINTR));
}

/* ---------------------------------------------------------------- */
/* by arch                                                          */

static inline uint64_t now_ns(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (uint64_t)ts.tv_sec * 1000000000ull + ts.tv_nsec;
}

static inline void cpu_relax(void)
{
#if defined(__x86_64__)
	__builtin_ia32_pause();
#elif defined(__aarch64__)
	__asm__ volatile("yield");
#endif
}

/*
 * Bulk copies into another guest's buffer can use non-temporal stores:
 * the destination is not in this CPU's cache, and streaming stores skip
 * reading each destination line first.  AVX2 on x86_64; elsewhere there
 * is no such path yet and plat_have_nt stays false.
 */
static bool plat_have_nt;

#if defined(__x86_64__)
__attribute__((target("avx2")))
static void plat_copy_nt(uint8_t *dst, const uint8_t *src, uint64_t n)
{
	uint64_t head = (32 - ((uintptr_t)dst & 31)) & 31;

	if (head > n)
		head = n;
	memcpy(dst, src, head);
	dst += head;
	src += head;
	n -= head;
	while (n >= 128) {
		__m256i a = _mm256_loadu_si256((const __m256i *)src);
		__m256i b = _mm256_loadu_si256((const __m256i *)(src + 32));
		__m256i c = _mm256_loadu_si256((const __m256i *)(src + 64));
		__m256i d = _mm256_loadu_si256((const __m256i *)(src + 96));

		_mm256_stream_si256((__m256i *)dst, a);
		_mm256_stream_si256((__m256i *)(dst + 32), b);
		_mm256_stream_si256((__m256i *)(dst + 64), c);
		_mm256_stream_si256((__m256i *)(dst + 96), d);
		dst += 128;
		src += 128;
		n -= 128;
	}
	memcpy(dst, src, n);
}

/* order streaming stores before what follows */
static inline void plat_nt_fence(void)
{
	_mm_sfence();
}

static inline void plat_init(void)
{
	__builtin_cpu_init();
	plat_have_nt = __builtin_cpu_supports("avx2") &&
		       !getenv("SHMRDMA_NO_NT");
}
#else
static inline void plat_copy_nt(uint8_t *dst, const uint8_t *src, uint64_t n)
{
	memcpy(dst, src, n);
}

static inline void plat_nt_fence(void)
{
}

static inline void plat_init(void)
{
}
#endif

#endif /* SHMRDMA_PLAT_H */
