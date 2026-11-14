/* SPDX-License-Identifier: GPL-2.0 */
/*
 * shmrdmad platform layer, macOS: kqueue, pipes, a small spinlock.
 *
 * QEMU's notifiers off Linux are non-blocking pipe pairs: the daemon
 * gets the read end of each kick and the write end of each call.
 */
#ifndef SHMRDMA_PLAT_MACOS_H
#define SHMRDMA_PLAT_MACOS_H

#include <sys/event.h>
#include <sys/time.h>

#define PLAT_MAX_EVENTS	64
#define PLAT_MSG_NOSIGNAL 0	/* none on macOS; SIGPIPE is ignored */

static inline void plat_cloexec(int fd)
{
	fcntl(fd, F_SETFD, FD_CLOEXEC);
}

static inline int plat_poller_new(void)
{
	int kq = kqueue();

	if (kq >= 0)
		plat_cloexec(kq);
	return kq;
}

/* EV_CLEAR is kqueue's edge trigger; EV_ADD of a known fd updates it */
static inline int plat_poller_add(int poller, int fd, void *tag, bool edge)
{
	struct kevent kev;

	EV_SET(&kev, fd, EVFILT_READ, EV_ADD | EV_ENABLE | (edge ? EV_CLEAR : 0),
	       0, 0, tag);
	return kevent(poller, &kev, 1, NULL, 0, NULL) ? -1 : 0;
}

static inline void plat_poller_del(int poller, int fd)
{
	struct kevent kev;

	EV_SET(&kev, fd, EVFILT_READ, EV_DELETE, 0, 0, NULL);
	(void)kevent(poller, &kev, 1, NULL, 0, NULL);
}

static inline int plat_poller_wait(int poller, struct plat_event *evs,
				   int max, int timeout_ms)
{
	struct kevent e[PLAT_MAX_EVENTS];
	struct timespec ts = {
		.tv_sec = timeout_ms / 1000,
		.tv_nsec = (long)(timeout_ms % 1000) * 1000000,
	};
	int n;

	if (max > PLAT_MAX_EVENTS)
		max = PLAT_MAX_EVENTS;
	n = kevent(poller, NULL, 0, e, max, timeout_ms < 0 ? NULL : &ts);
	for (int i = 0; i < n; i++) {
		evs[i].tag = e[i].udata;
		evs[i].hup = e[i].flags & (EV_EOF | EV_ERROR);
	}
	return n;
}

/* a pipe stands in for an eventfd: read end returned */
static inline int plat_notifier_new(int *wfd)
{
	int p[2];

	if (pipe(p))
		return -1;
	for (int i = 0; i < 2; i++) {
		plat_cloexec(p[i]);
		fcntl(p[i], F_SETFL, fcntl(p[i], F_GETFL) | O_NONBLOCK);
	}
	*wfd = p[1];
	return p[0];
}

/* test-and-test-and-set; held only around short ring updates */
typedef int plat_spinlock_t;

static inline void plat_spin_init(plat_spinlock_t *l)
{
	*l = 0;
}

static inline void plat_spin_lock(plat_spinlock_t *l)
{
	while (__atomic_exchange_n(l, 1, __ATOMIC_ACQUIRE))
		while (__atomic_load_n(l, __ATOMIC_RELAXED))
			__asm__ volatile("yield");
}

static inline void plat_spin_unlock(plat_spinlock_t *l)
{
	__atomic_store_n(l, 0, __ATOMIC_RELEASE);
}

static inline void plat_spin_destroy(plat_spinlock_t *l)
{
	(void)l;
}

static inline int plat_unix_socket(void)
{
	int s = socket(AF_UNIX, SOCK_STREAM, 0);

	if (s >= 0)
		plat_cloexec(s);
	return s;
}

static inline int plat_accept(int lsock)
{
	int s = accept(lsock, NULL, NULL);

	if (s >= 0)
		plat_cloexec(s);
	return s;
}

/* no MSG_CMSG_CLOEXEC: mark each received fd by hand */
static inline ssize_t plat_recvmsg(int sock, struct msghdr *mh)
{
	ssize_t n = recvmsg(sock, mh, 0);

	if (n <= 0)
		return n;
	for (struct cmsghdr *cm = CMSG_FIRSTHDR(mh); cm;
	     cm = CMSG_NXTHDR(mh, cm)) {
		if (cm->cmsg_level != SOL_SOCKET || cm->cmsg_type != SCM_RIGHTS)
			continue;
		int nfds = (cm->cmsg_len - CMSG_LEN(0)) / sizeof(int);

		for (int i = 0; i < nfds; i++) {
			int fd;

			memcpy(&fd, CMSG_DATA(cm) + i * sizeof(int), sizeof(fd));
			plat_cloexec(fd);
		}
	}
	return n;
}

#endif /* SHMRDMA_PLAT_MACOS_H */
