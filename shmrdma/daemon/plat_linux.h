/* SPDX-License-Identifier: GPL-2.0 */
/* shmrdmad platform layer, Linux: epoll, eventfd, pthread spinlocks. */
#ifndef SHMRDMA_PLAT_LINUX_H
#define SHMRDMA_PLAT_LINUX_H

#include <pthread.h>
#include <sys/epoll.h>
#include <sys/eventfd.h>

#define PLAT_MAX_EVENTS	64
#define PLAT_MSG_NOSIGNAL MSG_NOSIGNAL

static inline int plat_poller_new(void)
{
	return epoll_create1(EPOLL_CLOEXEC);
}

static inline int plat_poller_add(int poller, int fd, void *tag, bool edge)
{
	struct epoll_event ev = {
		.events = EPOLLIN | (edge ? EPOLLET : 0),
		.data.ptr = tag,
	};

	if (epoll_ctl(poller, EPOLL_CTL_ADD, fd, &ev) && errno != EEXIST)
		return -1;
	return 0;
}

static inline void plat_poller_del(int poller, int fd)
{
	epoll_ctl(poller, EPOLL_CTL_DEL, fd, NULL);
}

static inline int plat_poller_wait(int poller, struct plat_event *evs,
				   int max, int timeout_ms)
{
	struct epoll_event e[PLAT_MAX_EVENTS];
	int n;

	if (max > PLAT_MAX_EVENTS)
		max = PLAT_MAX_EVENTS;
	n = epoll_wait(poller, e, max, timeout_ms);
	for (int i = 0; i < n; i++) {
		evs[i].tag = e[i].data.ptr;
		evs[i].hup = e[i].events & (EPOLLHUP | EPOLLERR);
	}
	return n;
}

/* an eventfd is its own write end */
static inline int plat_notifier_new(int *wfd)
{
	int fd = eventfd(0, EFD_CLOEXEC | EFD_NONBLOCK);

	*wfd = fd;
	return fd;
}

typedef pthread_spinlock_t plat_spinlock_t;
#define plat_spin_init(l)	pthread_spin_init((l), 0)
#define plat_spin_lock(l)	pthread_spin_lock(l)
#define plat_spin_unlock(l)	pthread_spin_unlock(l)
#define plat_spin_destroy(l)	pthread_spin_destroy(l)

static inline int plat_unix_socket(void)
{
	return socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
}

static inline int plat_accept(int lsock)
{
	return accept4(lsock, NULL, NULL, SOCK_CLOEXEC);
}

static inline ssize_t plat_recvmsg(int sock, struct msghdr *mh)
{
	return recvmsg(sock, mh, MSG_CMSG_CLOEXEC);
}

#endif /* SHMRDMA_PLAT_LINUX_H */
