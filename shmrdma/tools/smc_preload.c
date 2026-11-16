/*
 * LD_PRELOAD shim: TCP sockets become SMC sockets (IPPROTO_SMC), so an
 * unmodified program such as iperf3 runs over SMC-R or SMC-D.
 *
 *   gcc -shared -fPIC -O2 -o smc_preload.so smc_preload.c -ldl
 *   LD_PRELOAD=./smc_preload.so iperf3 -s
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <netinet/in.h>
#include <sys/socket.h>

#ifndef IPPROTO_SMC
#define IPPROTO_SMC 256
#endif

int socket(int domain, int type, int protocol)
{
	static int (*real)(int, int, int);

	if (!real)
		real = (int (*)(int, int, int))dlsym(RTLD_NEXT, "socket");
	if ((domain == AF_INET || domain == AF_INET6) &&
	    (type & 0xf) == SOCK_STREAM &&
	    (protocol == 0 || protocol == IPPROTO_TCP))
		protocol = IPPROTO_SMC;
	return real(domain, type, protocol);
}
